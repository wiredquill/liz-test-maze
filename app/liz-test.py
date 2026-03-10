#!/usr/bin/env python3
"""
Liz AI Agent Test Runner (CLI)

Sends questions to the Rancher AI agent via WebSocket, measures TTFT and
total response time, and logs results to JSON/CSV.

Usage:
  python liz-test.py                           # Run with config/tests.yaml
  python liz-test.py --config custom.yaml      # Custom test config
  python liz-test.py --label "qwen3.5-27b"     # Label this run for comparison
  python liz-test.py --tests crash,broken-image  # Run specific tests by name
"""

import asyncio
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Missing dependency: pip install pyyaml")
    sys.exit(1)

try:
    import websockets  # noqa: F401
except ImportError:
    print("Missing dependency: pip install websockets")
    sys.exit(1)

from runner import run_test_suite, get_token_from_kubeconfig, WS_URL

KUBECONFIG = os.path.expanduser("~/.kube/liz.yaml")


def fmt_time(val):
    return f"{val:.2f}s" if val is not None else "N/A"


def print_summary_table(summary: dict):
    questions = summary.get("questions", [])
    if not questions:
        return
    print(f"\n{'='*60}")
    print(f"TIMING SUMMARY  (label: {summary.get('run_label', 'unlabeled')})")
    print(f"{'='*60}")
    print(f"{'Test':<35} {'Success':>7} {'TTFT avg':>9} {'Total avg':>10}")
    print(f"{'-'*35} {'-'*7} {'-'*9} {'-'*10}")
    for q in questions:
        name = q["name"][:34]
        ok = f"{q['success_count']}/{q['repetitions']}"
        ttft = fmt_time(q["ttft_stats"].get("avg"))
        total = fmt_time(q["total_time_stats"].get("avg"))
        print(f"{name:<35} {ok:>7} {ttft:>9} {total:>10}")


def main():
    parser = argparse.ArgumentParser(description="Test the Liz (Rancher AI) agent")
    parser.add_argument("--config", default="config/tests.yaml", help="Test config file (default: config/tests.yaml)")
    parser.add_argument("--label", default="", help="Label this run for comparison (e.g. model name)")
    parser.add_argument("--results-dir", default="results", help="Base directory for results (default: results/)")
    parser.add_argument("--tests", help="Comma-separated list of test names to run (default: all)")
    parser.add_argument("--timeout", type=float, default=120, help="Per-query timeout in seconds (default: 120)")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        # Try relative to repo root (one level up from app/)
        config_path = Path(__file__).parent.parent / args.config
    if not config_path.exists():
        print(f"Config file not found: {args.config}")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)
    config["_source"] = str(config_path)
    config["_kubeconfig"] = KUBECONFIG

    # Token resolution: config file > environment variable > kubeconfig
    if not config.get("token"):
        config["token"] = os.environ.get("LIZ_TOKEN") or get_token_from_kubeconfig(KUBECONFIG)
    if not config.get("token"):
        print("Error: no token found. Set 'token' in config, LIZ_TOKEN env var, or ensure ~/.kube/liz.yaml has a token.")
        sys.exit(1)

    test_filter = [t.strip() for t in args.tests.split(",")] if args.tests else None

    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    label_slug = f"-{args.label.replace(' ', '_')}" if args.label else ""
    results_dir = Path(args.results_dir) / f"{ts}{label_slug}"

    try:
        print(f"\nConfig:  {config_path}")
        if args.label:
            print(f"Label:   {args.label}")
        print(f"Results: {results_dir}/")
        print(f"Agent:   {WS_URL}")

        summary = asyncio.run(run_test_suite(
            config=config,
            results_dir=results_dir,
            label=args.label,
            test_filter=test_filter,
            timeout=args.timeout,
        ))
        print_summary_table(summary)

    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
