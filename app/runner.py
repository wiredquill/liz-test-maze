"""
Core test runner logic — used by both the CLI (liz-test.py) and the web server.
"""

import asyncio
import base64
import csv
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import websockets
import yaml

KUBECONFIG_DEFAULT = os.path.expanduser("~/.kube/liz.yaml")
KUBE_CONTEXT = "rancher"
RANCHER_HOST = os.environ.get("RANCHER_HOST", "liz.dna-42.com")
WS_URL = (
    f"wss://{RANCHER_HOST}/k8s/clusters/local/api/v1/namespaces/"
    f"cattle-ai-agent-system/services/http:rancher-ai-agent:80/proxy/v1/ws/messages"
)


def get_token_from_kubeconfig(kubeconfig_path: str = KUBECONFIG_DEFAULT) -> str | None:
    try:
        with open(kubeconfig_path) as f:
            kc = yaml.safe_load(f)
        for user in kc.get("users", []):
            token = user.get("user", {}).get("token")
            if token:
                return token
    except Exception:
        pass
    return None


def resolve_cluster_id(name_or_id: str, kubeconfig: str = KUBECONFIG_DEFAULT) -> str:
    if name_or_id.startswith("c-") or name_or_id == "local":
        return name_or_id
    try:
        result = subprocess.run(
            ["kubectl", f"--kubeconfig={kubeconfig}", f"--context={KUBE_CONTEXT}",
             "get", "clusters.management.cattle.io",
             "-o", "jsonpath={range .items[*]}{.metadata.name},{.spec.displayName}\\n{end}"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            cid, display = line.split(",", 1)
            if display.lower() == name_or_id.lower():
                return cid
    except Exception:
        pass
    return name_or_id


def get_llm_config(kubeconfig: str = KUBECONFIG_DEFAULT) -> dict:
    try:
        cm = subprocess.run(
            ["kubectl", f"--kubeconfig={kubeconfig}", f"--context={KUBE_CONTEXT}",
             "get", "configmap", "llm-config", "-n", "cattle-ai-agent-system",
             "-o", "jsonpath={.data}"],
            capture_output=True, text=True, timeout=10
        )
        data = json.loads(cm.stdout) if cm.returncode == 0 and cm.stdout else {}
        sec = subprocess.run(
            ["kubectl", f"--kubeconfig={kubeconfig}", f"--context={KUBE_CONTEXT}",
             "get", "secret", "llm-secret", "-n", "cattle-ai-agent-system",
             "-o", "jsonpath={.data.OLLAMA_URL}"],
            capture_output=True, text=True, timeout=10
        )
        if sec.returncode == 0 and sec.stdout:
            data["OLLAMA_URL"] = base64.b64decode(sec.stdout).decode()
        return data
    except Exception:
        return {}


async def run_query(token: str, message: str, agent_id: str = "rancher", context: dict = None, timeout: float = 120) -> dict:
    cookies = {"R_SESS": token}
    result = {
        "message": message,
        "agent_id": agent_id,
        "context": context or {},
        "success": False,
        "ttft": None,
        "total_time": None,
        "response": "",
        "chat_id": None,
        "error": None,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    try:
        async with websockets.connect(
            WS_URL,
            additional_headers={
                "Authorization": f"Bearer {token}",
                "Cookie": f"R_SESS={token}",
            },
            open_timeout=15,
            ping_interval=30,
            ping_timeout=30,
        ) as ws:
            payload = json.dumps({"prompt": message, "agent": agent_id, "context": context or {}})
            send_time = time.perf_counter()
            await ws.send(payload)

            in_message = False
            full_text = ""
            ttft = None

            while True:
                try:
                    chunk = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    now = time.perf_counter()
                    elapsed = now - send_time

                    if chunk.startswith("<chat-metadata>"):
                        try:
                            meta_str = chunk.replace("<chat-metadata>", "").replace("</chat-metadata>", "")
                            meta = json.loads(meta_str)
                            result["chat_id"] = meta.get("chatId")
                        except Exception:
                            pass

                    elif chunk == "<message>":
                        in_message = True

                    elif chunk == "</message>":
                        result["total_time"] = round(elapsed, 3)
                        result["response"] = full_text
                        result["success"] = True
                        break

                    elif chunk.startswith("<error>"):
                        result["error"] = chunk
                        result["response"] = full_text
                        break

                    elif in_message:
                        if ttft is None:
                            ttft = round(elapsed, 3)
                            result["ttft"] = ttft
                        full_text += chunk

                except asyncio.TimeoutError:
                    result["error"] = f"Timeout after {timeout}s"
                    if full_text:
                        result["response"] = full_text
                        result["success"] = True
                    break

                except websockets.exceptions.ConnectionClosed as e:
                    result["error"] = f"Connection closed: {e}"
                    if full_text:
                        result["response"] = full_text
                    break

    except Exception as e:
        result["error"] = str(e)

    return result


def fmt_time(val):
    return f"{val:.2f}s" if val is not None else "N/A"


def stats(vals):
    if not vals:
        return {"min": None, "max": None, "avg": None, "count": 0}
    return {
        "min": round(min(vals), 3),
        "max": round(max(vals), 3),
        "avg": round(sum(vals) / len(vals), 3),
        "count": len(vals),
    }


async def run_test_suite(
    config: dict,
    results_dir: Path,
    label: str = "",
    test_filter: list = None,
    timeout: float = 120,
    log_fn: Callable[[str], None] = None,
) -> dict:
    def log(msg: str):
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    token = config["token"]
    agent_id = config.get("agent_id", "rancher")
    default_reps = config.get("default_repetitions", 3)
    query_delay = config.get("delay_between_queries", 1)
    kubeconfig = config.get("_kubeconfig", KUBECONFIG_DEFAULT)

    tests = config.get("tests", [])
    if test_filter:
        tests = [t for t in tests if t["name"] in test_filter]
        if not tests:
            log(f"No tests matched filter: {test_filter}")
            return {}

    all_results = []
    llm_config = get_llm_config(kubeconfig)
    summary = {
        "run_label": label,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "config_file": str(config.get("_source", "")),
        "llm": {
            "active": llm_config.get("ACTIVE_LLM", "unknown"),
            "model": llm_config.get("OLLAMA_MODEL") or llm_config.get("OPENAI_MODEL") or llm_config.get("GEMINI_MODEL") or "unknown",
            "ollama_url": llm_config.get("OLLAMA_URL", ""),
        },
        "agent_config": {
            "agent_id": agent_id,
            "default_repetitions": default_reps,
        },
        "questions": [],
    }

    total_tests = len(tests)
    for test_idx, test in enumerate(tests, 1):
        name = test["name"]
        message = test["message"]
        repetitions = test.get("repetitions", default_reps)
        test_agent_id = test.get("agent_id", agent_id)

        log(f"[{test_idx}/{total_tests}] {name}")
        log(f"  Q: {message[:90]}{'...' if len(message) > 90 else ''}")
        log(f"  Runs: {repetitions}")

        question_results = []
        ttfts = []
        totals = []

        test_context = dict(test.get("context", {}))
        if "clusterId" in test_context:
            test_context["clusterId"] = resolve_cluster_id(test_context["clusterId"], kubeconfig)

        for i in range(1, repetitions + 1):
            result = await run_query(token, message, test_agent_id, context=test_context, timeout=timeout)
            result["test_name"] = name
            result["run_number"] = i

            status = "OK" if result["success"] else "FAIL"
            ttft_s = fmt_time(result["ttft"])
            total_s = fmt_time(result["total_time"])
            line = f"  Run {i}/{repetitions}: [{status}] TTFT={ttft_s}  Total={total_s}"
            if result["error"]:
                line += f"  Error: {result['error']}"
            log(line)

            question_results.append(result)
            all_results.append(result)

            if result["ttft"] is not None:
                ttfts.append(result["ttft"])
            if result["total_time"] is not None:
                totals.append(result["total_time"])

            if i < repetitions:
                await asyncio.sleep(query_delay)

        ttft_stats = stats(ttfts)
        total_stats = stats(totals)
        success_count = sum(1 for r in question_results if r["success"])

        log(f"  Summary: {success_count}/{repetitions} succeeded")
        if ttft_stats["count"] > 0:
            log(f"  TTFT:  min={fmt_time(ttft_stats['min'])}  avg={fmt_time(ttft_stats['avg'])}  max={fmt_time(ttft_stats['max'])}")
        if total_stats["count"] > 0:
            log(f"  Total: min={fmt_time(total_stats['min'])}  avg={fmt_time(total_stats['avg'])}  max={fmt_time(total_stats['max'])}")

        summary["questions"].append({
            "name": name,
            "message": message,
            "repetitions": repetitions,
            "success_count": success_count,
            "ttft_stats": ttft_stats,
            "total_time_stats": total_stats,
        })

    # Save results
    results_dir.mkdir(parents=True, exist_ok=True)

    with open(results_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    with open(results_dir / "results.json", "w") as f:
        json.dump(all_results, f, indent=2)

    csv_file = results_dir / "results.csv"
    fields = ["timestamp", "test_name", "run_number", "success", "ttft", "total_time", "error", "response"]
    with open(csv_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in all_results:
            writer.writerow(r)

    log(f"{'='*60}")
    log(f"Results saved to: {results_dir}/")

    return summary
