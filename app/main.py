#!/usr/bin/env python3
"""
Liz Test Maze — Web server
Provides a UI to configure and launch Liz AI agent tests with live streaming output.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Import the test runner logic
from runner import run_test_suite, get_token_from_kubeconfig, get_llm_config, resolve_cluster_id

app = FastAPI(title="Liz Test Maze")

RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "/results"))
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/config/tests.yaml"))
KUBECONFIG = os.path.expanduser(os.environ.get("KUBECONFIG", "~/.kube/liz.yaml"))

# In-memory run registry: run_id -> RunState
runs: dict[str, dict] = {}


# ── Models ────────────────────────────────────────────────────────────────────

class StartRunRequest(BaseModel):
    label: str = ""
    tags: dict[str, str] = {}   # extra key/value pairs included in the label (e.g. gpu, window_size)
    selected_tests: list[str] = []  # empty = run all
    timeout: float = 120.0


# ── Config helpers ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load tests.yaml from ConfigMap mount (re-read on every call for hot-reload)."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    cfg["_source"] = str(CONFIG_PATH)
    return cfg


def get_token() -> str:
    token = os.environ.get("LIZ_TOKEN") or get_token_from_kubeconfig(KUBECONFIG)
    if not token:
        raise RuntimeError("No token found. Set LIZ_TOKEN env var or provide ~/.kube/liz.yaml")
    return token


# ── API routes ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    static_index = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(static_index.read_text())


@app.get("/api/config")
async def api_config():
    """Return current config: list of tests, cluster, agent settings."""
    try:
        cfg = load_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    tests = [
        {"name": t["name"], "message": t.get("message", ""), "repetitions": t.get("repetitions", cfg.get("default_repetitions", 3))}
        for t in cfg.get("tests", [])
    ]
    return {
        "agent_id": cfg.get("agent_id", "rancher"),
        "cluster_id": cfg.get("cluster_id", ""),
        "broken_ns": cfg.get("broken_ns", "broken"),
        "default_repetitions": cfg.get("default_repetitions", 3),
        "tests": tests,
    }


@app.get("/api/llm")
async def api_llm():
    """Return current LLM config from the cluster."""
    try:
        return get_llm_config(KUBECONFIG)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/runs")
async def start_run(req: StartRunRequest):
    """Start a new test run. Returns the run ID immediately; stream output via SSE."""
    try:
        config = load_config()
        token = get_token()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    config["token"] = token

    # Build full label from label + tags
    label_parts = [req.label] if req.label else []
    for k, v in req.tags.items():
        if v:
            label_parts.append(f"{k}:{v}")
    full_label = "-".join(label_parts)

    run_id = uuid.uuid4().hex[:12]
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    label_slug = f"-{full_label.replace(' ', '_')}" if full_label else ""
    run_dir = RESULTS_DIR / f"{ts}{label_slug}"

    run_state = {
        "id": run_id,
        "label": full_label,
        "status": "pending",
        "started_at": datetime.utcnow().isoformat() + "Z",
        "run_dir": str(run_dir),
        "log": [],
        "summary": None,
        "error": None,
    }
    runs[run_id] = run_state

    # Launch in background
    asyncio.create_task(_execute_run(run_id, config, run_dir, full_label, req.selected_tests or None, req.timeout))

    return {"run_id": run_id, "run_dir": str(run_dir)}


async def _execute_run(run_id: str, config: dict, run_dir: Path, label: str, test_filter: list | None, timeout: float):
    state = runs[run_id]
    state["status"] = "running"

    def log(msg: str):
        state["log"].append(msg)

    try:
        summary = await run_test_suite(
            config=config,
            results_dir=run_dir,
            label=label,
            test_filter=test_filter,
            timeout=timeout,
            log_fn=log,
        )
        state["summary"] = summary
        state["status"] = "done"
    except Exception as e:
        state["error"] = str(e)
        state["status"] = "error"
        log(f"ERROR: {e}")


@app.get("/api/runs")
async def list_runs():
    """List all runs (in-memory + any on disk not tracked in memory)."""
    result = []

    # In-memory runs first
    for run_id, state in runs.items():
        result.append({
            "id": run_id,
            "label": state["label"],
            "status": state["status"],
            "started_at": state["started_at"],
            "run_dir": state["run_dir"],
            "summary": state.get("summary"),
        })

    # Disk runs not in memory
    if RESULTS_DIR.exists():
        tracked_dirs = {state["run_dir"] for state in runs.values()}
        for d in sorted(RESULTS_DIR.iterdir(), reverse=True):
            if not d.is_dir() or str(d) in tracked_dirs:
                continue
            summary_file = d / "summary.json"
            if summary_file.exists():
                try:
                    summary = json.loads(summary_file.read_text())
                    result.append({
                        "id": d.name,
                        "label": summary.get("run_label", d.name),
                        "status": "done",
                        "started_at": summary.get("timestamp", ""),
                        "run_dir": str(d),
                        "summary": summary,
                    })
                except Exception:
                    pass

    return result


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    if run_id not in runs:
        raise HTTPException(status_code=404, detail="Run not found")
    state = runs[run_id]
    return {
        "id": run_id,
        "label": state["label"],
        "status": state["status"],
        "started_at": state["started_at"],
        "run_dir": state["run_dir"],
        "summary": state.get("summary"),
        "error": state.get("error"),
        "log_lines": len(state["log"]),
    }


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str):
    """SSE endpoint — streams log lines as they appear, then sends 'done' event."""
    if run_id not in runs:
        raise HTTPException(status_code=404, detail="Run not found")

    async def generate() -> AsyncGenerator[str, None]:
        state = runs[run_id]
        sent = 0
        while True:
            log = state["log"]
            while sent < len(log):
                line = log[sent]
                data = json.dumps({"line": line, "index": sent})
                yield f"data: {data}\n\n"
                sent += 1

            if state["status"] in ("done", "error"):
                # Flush any remaining lines
                log = state["log"]
                while sent < len(log):
                    line = log[sent]
                    data = json.dumps({"line": line, "index": sent})
                    yield f"data: {data}\n\n"
                    sent += 1
                # Send terminal event
                final = json.dumps({"done": True, "status": state["status"], "summary": state.get("summary"), "error": state.get("error")})
                yield f"event: done\ndata: {final}\n\n"
                return

            await asyncio.sleep(0.2)

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/log")
async def get_log(run_id: str, offset: int = 0):
    """Poll-friendly endpoint: return log lines from offset."""
    if run_id not in runs:
        raise HTTPException(status_code=404, detail="Run not found")
    state = runs[run_id]
    return {
        "lines": state["log"][offset:],
        "total": len(state["log"]),
        "status": state["status"],
    }


@app.get("/api/report")
async def html_report():
    """Generate and return an HTML report of all completed runs."""
    import subprocess
    import sys
    report_path = RESULTS_DIR / "report.html"
    script = Path(__file__).parent / "report.py"
    try:
        subprocess.run(
            [sys.executable, str(script), "--results-dir", str(RESULTS_DIR), "--out", str(report_path), "--list"],
            check=True, capture_output=True, text=True
        )
        # Generate HTML quietly
        subprocess.run(
            [sys.executable, str(script), "--results-dir", str(RESULTS_DIR), "--out", str(report_path)],
            check=False, capture_output=True, text=True
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="No results found")
    return HTMLResponse(report_path.read_text())


# Mount static files last so API routes take priority
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
