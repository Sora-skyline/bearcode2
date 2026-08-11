"""Evaluation orchestration, subprocess isolation, caching, and reporting."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .datasets import (
    REPO_ROOT,
    load_gaia_samples,
    load_manifest,
    load_skill_curriculum,
    load_toolhop_samples,
)
from .scorers import render_report, score_predictions


RUNS_ROOT = REPO_ROOT / ".bear" / "evals" / "runs"
SENSITIVE_KEY = re.compile(r"(api.?key|token|secret|password|authorization)", re.IGNORECASE)
SECRET_VALUE = re.compile(r"(?i)(bearer\s+)?sk-[a-z0-9_-]{8,}")


def redact_secrets(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]" if value not in (None, "") else value
    if isinstance(value, dict):
        return {name: redact_secrets(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE.sub("[REDACTED]", value)
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(redact_secrets(value), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return "unknown"


def _new_run_dir(suite: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return RUNS_ROOT / f"{stamp}-{suite}-{uuid.uuid4().hex[:8]}"


def _prediction_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid predictions JSONL at line {line_number}") from error
    return rows


def _write_predictions(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(redact_secrets(row), ensure_ascii=False) + "\n")
    temporary.replace(path)


def _job_specs(
    *,
    suite: str,
    preset: str,
    model: str,
    api_base: str | None,
    max_cost_usd: float | None,
    mcp_enabled: bool,
    judge_model: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    common = {
        "suite": suite,
        "preset": preset,
        "model": model,
        "api_base": api_base,
        "max_cost_usd": max_cost_usd,
        "max_turns": 12,
        "mcp_enabled": mcp_enabled,
        "judge_model": judge_model,
    }
    jobs: list[dict[str, Any]] = []
    data_ids: list[str] = []
    if suite == "skill":
        curriculum = load_skill_curriculum(preset)
        for phases in curriculum["families"].values():
            data_ids.extend(sample["id"] for sample in phases.values())
        data_ids.append(curriculum["unrelated"]["id"])
        for condition in ("skill_off", "oracle_static", "evolved_skill"):
            jobs.append({**common, "job_id": condition, "condition": condition})
    elif suite == "folding":
        samples = load_toolhop_samples(preset)
        data_ids.extend(sample["id"] for sample in samples)
        for sample in samples:
            for condition in ("folding_off", "folding_structured"):
                jobs.append({
                    **common,
                    "job_id": f"{condition}-{sample['id']}",
                    "condition": condition,
                    "sample": sample,
                })
    elif suite == "gaia":
        samples = load_gaia_samples(preset)
        data_ids.extend(sample["id"] for sample in samples)
        for sample in samples:
            for condition in ("folding_off", "folding_structured"):
                jobs.append({
                    **common,
                    "job_id": f"gaia-{condition}-{sample['id']}",
                    "condition": condition,
                    "sample": sample,
                })
    else:
        raise ValueError(f"unsupported eval suite: {suite}")
    return jobs, sorted(set(data_ids))


def _copy_mcp_config(workspace: Path, isolated_home: Path) -> None:
    pairs = [
        (Path.home() / ".bear" / "settings.json", isolated_home / ".bear" / "settings.json"),
        (REPO_ROOT / ".bear" / "settings.json", workspace / ".bear" / "settings.json"),
        (REPO_ROOT / ".mcp.json", workspace / ".mcp.json"),
    ]
    for source, destination in pairs:
        if not source.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _run_job(job: dict[str, Any], run_dir: Path, *, timeout_s: int) -> None:
    jobs_dir = run_dir / "jobs"
    results_dir = jobs_dir / "results"
    logs_dir = jobs_dir / "logs"
    workspace_root = run_dir / "workspaces"
    for directory in (jobs_dir, results_dir, logs_dir, workspace_root):
        directory.mkdir(parents=True, exist_ok=True)
    result_path = results_dir / f"{job['job_id']}.json"
    if result_path.is_file() and not _read_json(result_path).get("error"):
        return

    with tempfile.TemporaryDirectory(prefix=f"{job['job_id']}-", dir=workspace_root) as temporary:
        isolated_root = Path(temporary)
        workspace = isolated_root / "project"
        isolated_home = isolated_root / "home"
        workspace.mkdir()
        isolated_home.mkdir()
        if job.get("mcp_enabled"):
            _copy_mcp_config(workspace, isolated_home)
        complete_job = {
            **job,
            "repo_root": str(REPO_ROOT),
            "workspace": str(workspace),
            "result_path": str(result_path),
        }
        job_path = jobs_dir / f"{job['job_id']}.json"
        _write_json(job_path, complete_job)
        env = os.environ.copy()
        env["HOME"] = str(isolated_home)
        env["BEAR_AUTO_SKILL_TARGET"] = "project"
        env["BEAR_AUTO_SKILL_EVOLUTION"] = "1"
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            str(REPO_ROOT)
            if not existing_pythonpath
            else os.pathsep.join([str(REPO_ROOT), existing_pythonpath])
        )
        process = subprocess.run(
            [sys.executable, "-m", "evals.worker", str(job_path)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        log_text = (
            f"exit_code={process.returncode}\n\nSTDOUT\n{process.stdout}\n\nSTDERR\n{process.stderr}"
        )
        (logs_dir / f"{job['job_id']}.log").write_text(log_text, encoding="utf-8")
        if process.returncode != 0:
            detail = _read_json(result_path).get("error") if result_path.is_file() else process.stderr
            raise RuntimeError(f"eval worker {job['job_id']} failed: {detail}")


def _collect_job_rows(run_dir: Path, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job in jobs:
        result_path = run_dir / "jobs" / "results" / f"{job['job_id']}.json"
        if not result_path.is_file():
            continue
        payload = _read_json(result_path)
        if payload.get("error"):
            raise RuntimeError(f"cached eval worker {job['job_id']} failed: {payload['error']}")
        for row in payload.get("rows") or []:
            rows.append({"job_id": job["job_id"], **row})
    return rows


def score_run(run_dir: str | Path) -> dict[str, Any]:
    """Score only cached predictions; this function never constructs an Agent."""
    directory = Path(run_dir).resolve()
    config_path = directory / "config.json"
    predictions_path = directory / "predictions.jsonl"
    if not config_path.is_file() or not predictions_path.is_file():
        raise FileNotFoundError("run directory must contain config.json and predictions.jsonl")
    config = _read_json(config_path)
    rows = _prediction_rows(predictions_path)
    metrics = score_predictions(str(config["suite"]), rows)
    _write_json(directory / "metrics.json", metrics)
    (directory / "report.md").write_text(
        render_report(config, metrics), encoding="utf-8"
    )
    return metrics


def run_evaluation(
    *,
    suite: str,
    preset: str = "low-cost",
    model: str = "deepseek-chat",
    run_dir: str | Path | None = None,
    api_base: str | None = None,
    max_cost_usd: float | None = None,
    mcp_enabled: bool = False,
    judge_model: str | None = None,
    dry_run: bool = False,
    timeout_s: int = 3600,
) -> Path:
    load_manifest(preset)
    directory = Path(run_dir).resolve() if run_dir else _new_run_dir(suite).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    jobs, data_ids = _job_specs(
        suite=suite,
        preset=preset,
        model=model,
        api_base=api_base,
        max_cost_usd=max_cost_usd,
        mcp_enabled=mcp_enabled,
        judge_model=judge_model,
    )
    config_path = directory / "config.json"
    if config_path.is_file():
        config = _read_json(config_path)
        for key, expected in (("suite", suite), ("preset", preset), ("model", model)):
            if config.get(key) != expected:
                raise ValueError(
                    f"resume config mismatch for {key}: {config.get(key)!r} != {expected!r}"
                )
    else:
        config = {
            "run_id": directory.name,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "suite": suite,
            "preset": preset,
            "model": model,
            "judge_model": judge_model,
            "mcp_enabled": mcp_enabled,
            "git_sha": _git_sha(),
            "data_ids": data_ids,
            "conditions": sorted({str(job["condition"]) for job in jobs}),
            "budget": {
                "max_turns": 12,
                "max_cost_usd_per_agent": max_cost_usd,
                "effective_window_structured": 6000,
                "max_folds_structured": 1,
            },
            "generation_scoring_separated": True,
        }
        _write_json(config_path, config)

    if dry_run:
        _write_predictions(directory / "predictions.jsonl", [])
        _write_json(directory / "metrics.json", {
            "suite": suite,
            "dry_run": True,
            "planned_jobs": [job["job_id"] for job in jobs],
        })
        (directory / "report.md").write_text(
            f"# Bear Code Eval Dry Run: {suite}\n\nPlanned jobs: {len(jobs)}\n",
            encoding="utf-8",
        )
        return directory

    for job in jobs:
        _run_job(job, directory, timeout_s=timeout_s)
    rows = _collect_job_rows(directory, jobs)
    _write_predictions(directory / "predictions.jsonl", rows)
    score_run(directory)
    return directory
