"""Command-line interface for Bear Code evaluations."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .runner import run_evaluation, score_run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="generate predictions in isolated subprocesses")
    run.add_argument("--suite", required=True, choices=("skill", "folding", "gaia"))
    run.add_argument("--preset", default="low-cost", choices=("low-cost",))
    run.add_argument("--model", default=os.environ.get("MODEL") or "deepseek-chat")
    run.add_argument("--run-dir", help="resume or write to an explicit run directory")
    run.add_argument("--api-base", default=None)
    run.add_argument("--max-cost", type=float, default=None)
    run.add_argument("--mcp", action="store_true", help="enable MCP for optional GAIA validation")
    run.add_argument("--judge-model", default=None, help="explicitly enable an LLM answer judge")
    run.add_argument("--timeout", type=int, default=3600, help="seconds per isolated worker")
    run.add_argument("--dry-run", action="store_true", help="materialize the plan without model calls")

    score = commands.add_parser("score", help="score cached predictions without model calls")
    score.add_argument("--run-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "score":
        metrics = score_run(args.run_dir)
        print(json.dumps(metrics.get("conditions") or {}, ensure_ascii=False, indent=2))
        return 0

    directory = run_evaluation(
        suite=args.suite,
        preset=args.preset,
        model=args.model,
        run_dir=args.run_dir,
        api_base=args.api_base,
        max_cost_usd=args.max_cost,
        mcp_enabled=args.mcp,
        judge_model=args.judge_model,
        dry_run=args.dry_run,
        timeout_s=args.timeout,
    )
    print(str(Path(directory)))
    return 0
