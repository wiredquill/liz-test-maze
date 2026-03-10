# Liz Test Maze

Automated benchmark suite for the [Rancher AI agent (Liz)](https://github.com/rancher-sandbox/rancher-ai-agent).

Measures **time to first token (TTFT)** and **total response time**, captures full response text, and lets you compare results across LLM model changes.

Includes a **web UI** for launching tests, watching live output, and reviewing results — deployable on any Kubernetes cluster.

---

## Quick start (local)

```bash
# Install deps
pip install -r requirements.txt

# Run tests against the current model
python app/liz-test.py --label "qwen3.5-27b"

# View results in browser
python app/report.py
```

Requires Python 3.10+, `~/.kube/liz.yaml` with a valid Rancher token.

---

## Quick start (web UI local)

```bash
pip install -r requirements.txt

export LIZ_TOKEN=<your-token>
export RANCHER_HOST=liz.dna-42.com
export CONFIG_PATH=config/tests.yaml
export RESULTS_DIR=results

uvicorn app.main:app --port 8080 --reload
# Open http://localhost:8080
```

---

## Deploy on Kubernetes (Helm)

### Build and push the image

```bash
docker build -t ghcr.io/your-org/liz-test-maze:latest .
docker push ghcr.io/your-org/liz-test-maze:latest
```

### Install via Helm

```bash
helm install liz-test-maze ./helm/liz-test-maze \
  --namespace liz-test \
  --create-namespace \
  --set rancherHost=liz.dna-42.com \
  --set lizToken=<your-rancher-token> \
  --set ingress.enabled=true \
  --set ingress.host=liz-test.example.com
```

Or use a `values.yaml`:

```bash
helm install liz-test-maze ./helm/liz-test-maze -f my-values.yaml
```

The Helm chart creates:
- `Deployment` — web server + test runner
- `Service` — ClusterIP on port 8080
- `ConfigMap` — `tests.yaml` (hot-reloadable without restart)
- `Secret` — Rancher API token
- `PersistentVolumeClaim` — stores test results across restarts
- `Ingress` — optional, for external access

---

## Web UI

Open the app in your browser:

1. **Label** — a short name for this run (e.g. model name)
2. **Tags** — optional metadata: GPU type, context window size, notes
3. **Tests** — check/uncheck which tests to include
4. Click **▶ Start Test Run**
5. Watch live output in the **Live Output** tab
6. Review timing tables in the **Results** tab
7. Click **View HTML Report** for a side-by-side comparison across all runs

---

## Changing the LLM

```bash
# Switch Ollama model
./app/set-model.sh --model qwen3.5:27b

# Switch to a different Ollama server
./app/set-model.sh --model llama3.3:70b --url http://10.9.0.105:11434

# Switch to OpenAI
./app/set-model.sh --llm openai --key sk-...
```

After switching, start a new run from the web UI or CLI with an appropriate label.

---

## CLI usage

```bash
# All tests
python app/liz-test.py --label "llama3.3-70b"

# Specific tests
python app/liz-test.py --tests crash-loop-diagnosis,broken-image-diagnosis --label "debug"

# Custom config
python app/liz-test.py --config config/tests.yaml --label "h100-gpt-oss-120b"

# Generate HTML report
python app/report.py

# Compare two runs
python app/compare.py results/run-a results/run-b
```

---

## Baseline

The target baseline run is **gpt-oss:120b on H100** — all future model comparisons will be measured against this.

To capture it:

```bash
./app/set-model.sh --model gpt-oss:120b --url http://<h100-ip>:11434
python app/liz-test.py --label "gpt-oss-120b-h100"
```

Then any new run will show % delta vs this baseline in the HTML report.

---

## Test cases

| Test | What it checks |
|------|----------------|
| `general-hello` | Basic greeting / capability overview |
| `list-clusters` | Can Liz list Rancher clusters |
| `broken-namespace-overview` | Diagnoses all failing deployments at once |
| `crash-loop-diagnosis` | CrashLoopBackOff (container exits immediately) |
| `broken-image-diagnosis` | ImagePullBackOff (invalid image tag) |
| `impossible-schedule-diagnosis` | Pending pod due to unsatisfiable node affinity |
| `missing-config-diagnosis` | CreateContainerConfigError (missing ConfigMap) |
| `readonly-filesystem-diagnosis` | CrashLoop due to write to read-only mount |
| `bad-probe-diagnosis` | Liveness probe port mismatch |
| `resource-rightsizing-broad` | Finds over-provisioned deployments unprompted |
| `resource-rightsizing-focused` | Compares allocated vs actual usage, recommends new values |
| `fix-crash-loop` | Asks for fix steps, not just diagnosis |
| `fix-broken-image` | Asks for fix steps, not just diagnosis |

The **resource rightsizing** tests are the primary model quality benchmark — they require multi-step reasoning (list deployments → fetch metrics → compare → recommend).

---

## Results structure

```
results/
  2026-03-09T11-50-33-qwen3.5-27b/
    summary.json   # Timing stats per question + LLM config at run time
    results.json   # Full response text + raw timing for every run
    results.csv    # Spreadsheet-friendly format
```

`summary.json` captures the active model and Ollama URL so you don't have to rely on the folder name to know which model was tested.

---

## Demo deployments

The `demo/` directory contains broken Kubernetes deployments for testing Liz against:

- `crash-loop.yaml` — container exits immediately
- `broken-image.yaml` — invalid image tag
- `impossible-schedule.yaml` — unsatisfiable node affinity
- `missing-config.yaml` — missing ConfigMap reference
- `readonly-filesystem.yaml` — write to read-only mount
- `bad-probe.yaml` — liveness probe port mismatch
- `resource-hog.yaml` — over-provisioned resources (for rightsizing tests)
- `redis.yaml` — working deployment (resource comparison baseline)

Deploy them with:

```bash
kubectl apply -f demo/ --namespace broken
```

See [demo/Demos.md](demo/Demos.md) for details on each scenario.

---

## Files

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI web server with SSE streaming |
| `app/runner.py` | Core async test runner (shared by CLI and web server) |
| `app/liz-test.py` | CLI test runner |
| `app/report.py` | HTML report generator |
| `app/compare.py` | Side-by-side timing comparison of two runs |
| `app/set-model.sh` | Switch LLM model/URL and restart agent |
| `app/static/index.html` | Web UI |
| `config/tests.yaml` | Default test cases and config |
| `Dockerfile` | SUSE BCI Python 3.11 container |
| `helm/liz-test-maze/` | Helm chart |
| `demo/` | Broken deployment manifests |
