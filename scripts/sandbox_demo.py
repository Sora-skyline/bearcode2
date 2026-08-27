#!/usr/bin/env python3
"""运行 Bear Code Sandbox 的可重复面试演示并生成 JSON 报告。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time

from agents.sandbox import ExecRequest, SandboxSession, load_sandbox_config


async def run_demo(report_path: Path) -> dict:
    events: list[dict] = []

    def record(event_type: str, **kwargs) -> None:
        events.append({"type": event_type, **kwargs})

    session = SandboxSession(load_sandbox_config(), event_sink=record)
    cases: list[dict] = []
    cold_started = time.perf_counter()
    try:
        await session.start()
        cold_start_ms = int((time.perf_counter() - cold_started) * 1000)

        async def check(
            name: str,
            command: str,
            expected_exit: int | None = 0,
            timeout: float | None = None,
        ) -> None:
            result = await session.exec(ExecRequest(command=command, timeout_seconds=timeout))
            cases.append(
                {
                    "name": name,
                    "passed": result.exit_code != 0 if expected_exit is None else result.exit_code == expected_exit,
                    "exitCode": result.exit_code,
                    "durationMs": result.duration_ms,
                    "timedOut": result.timed_out,
                }
            )

        await check("non-root uid", "test \"$(id -u)\" != 0")
        await check("workspace read-write", "printf sandbox-demo > .bear-sandbox-probe && test -f .bear-sandbox-probe")
        await check("no docker socket", "test ! -e /var/run/docker.sock")
        await check("no host home mount", "test ! -e /host-home && test ! -e /Users")
        await check(
            "no inherited API secret",
            "! env | grep -E '^(APIKEY|OPENAI_API_KEY|ANTHROPIC_API_KEY)='",
        )
        await check(
            "network disabled",
            "curl --connect-timeout 1 --max-time 2 https://example.com >/dev/null 2>&1",
            expected_exit=None,
            timeout=3,
        )

        warm_ms = []
        for _ in range(10):
            result = await session.exec(ExecRequest(command="pwd"))
            warm_ms.append(result.duration_ms)

        timeout_result = await session.exec(ExecRequest(command="sleep 5", timeout_seconds=0.2))
        clean_result = await session.exec(ExecRequest(command="! pgrep -f '[s]leep 5'"))
        cases.append(
            {
                "name": "timeout restarts and cleans process tree",
                "passed": timeout_result.timed_out and clean_result.exit_code == 0,
                "exitCode": timeout_result.exit_code,
                "durationMs": timeout_result.duration_ms,
                "timedOut": timeout_result.timed_out,
            }
        )

        ordered = sorted(warm_ms)
        p95_index = max(0, min(len(ordered) - 1, round(0.95 * len(ordered) + 0.5) - 1))
        report = {
            "sandboxId": session.sandbox_id,
            "config": session.config.event_limits(),
            "coldStartMs": cold_start_ms,
            "warmCommandP95Ms": ordered[p95_index],
            "warmCommandMeanMs": round(statistics.mean(warm_ms), 2),
            "attackCasesPassed": sum(1 for case in cases if case["passed"]),
            "attackCasesTotal": len(cases),
            "cases": cases,
            "eventTypes": sorted({event["type"] for event in events}),
            "limitations": [
                "single-user local demo; not a multi-tenant security boundary",
                "workspace is bind-mounted read-write; no copy-on-write review step",
                "Docker backend only; no microVM, remote provider or snapshot support",
            ],
        }
    finally:
        await session.close()
        probe = Path.cwd() / ".bear-sandbox-probe"
        if probe.exists():
            probe.unlink()

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path(".bear/sandbox-evaluation.json"))
    args = parser.parse_args()
    report = asyncio.run(run_demo(args.report))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nReport written to {args.report}")


if __name__ == "__main__":
    main()
