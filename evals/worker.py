"""Isolated subprocess worker for Bear Code evaluations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any, Callable


def _slug(value: str) -> str:
    value = re.sub(r"(?<!^)(?=[A-Z])", "-", value).lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _skill_tool_definitions() -> list[dict[str, Any]]:
    from agents.tools import tool_definitions

    wanted = {"skill", "skill_create", "skill_evolve"}
    return [tool for tool in tool_definitions if tool.get("name") in wanted]


def _compact_tool_definition() -> dict[str, Any]:
    from agents.tools import tool_definitions

    return next(tool for tool in tool_definitions if tool.get("name") == "compact_context")


def _reset_skill_cache() -> None:
    from agents.skills import reset_skill_cache

    reset_skill_cache()


def _agent_factory(
    job: dict[str, Any],
    *,
    tools: list[dict[str, Any]] | None,
    custom_executor: Callable[[str, dict[str, Any]], Any] | None,
    skill_mode: str,
    folding_mode: str,
    effective_window: int | None = None,
    max_folds: int | None = None,
    mcp_enabled: bool = False,
    model: str | None = None,
):
    from agents.agent import Agent, RuntimeFeatures
    from agents.main import _resolve_api_config

    api_base, api_key, use_openai = _resolve_api_config(job.get("api_base"))
    if not api_key:
        raise RuntimeError("API key is required for eval generation")
    if use_openai and not api_base:
        api_base = "https://api.openai.com/v1"
    features = RuntimeFeatures(
        skill_mode=skill_mode,
        folding_mode=folding_mode,
        effective_window_override=effective_window,
        max_folds=max_folds,
        mcp_enabled=mcp_enabled,
        auto_save=False,
        temperature=0.0,
    )
    return Agent(
        permission_mode="bypassPermissions",
        model=model or str(job["model"]),
        api_base=api_base if use_openai else None,
        anthropic_base_url=api_base if not use_openai else None,
        api_key=api_key,
        max_cost_usd=job.get("max_cost_usd"),
        max_turns=int(job.get("max_turns") or 12),
        custom_tools=tools,
        custom_tool_executor=custom_executor,
        runtime_features=features,
    )


def _api_executor(sample: dict[str, Any]):
    gold_by_name: dict[str, list[dict[str, Any]]] = {}
    for call in sample.get("gold_calls") or []:
        gold_by_name.setdefault(str(call.get("name") or ""), []).append(call)
    positions: dict[str, int] = {}

    async def execute(name: str, arguments: dict[str, Any]) -> str:
        candidates = gold_by_name.get(name) or []
        exact = next((call for call in candidates if call.get("arguments") == arguments), None)
        if exact is not None:
            return json.dumps(exact.get("result"), ensure_ascii=False)
        position = positions.get(name, 0)
        positions[name] = position + 1
        expected = candidates[min(position, len(candidates) - 1)] if candidates else None
        return json.dumps({
            "error": "arguments_do_not_match_gold",
            "received": arguments,
            "expected_schema_only": sorted((expected or {}).get("arguments", {}).keys()),
        }, ensure_ascii=False)

    return execute


def _toolhop_executor(sample: dict[str, Any]):
    positions: dict[str, int] = {}

    async def execute(name: str, arguments: dict[str, Any]) -> str:
        candidates = (sample.get("tool_results") or {}).get(name) or []
        if not candidates:
            return json.dumps({"error": "unknown simulated tool"}, ensure_ascii=False)
        position = positions.get(name, 0)
        positions[name] = position + 1
        observation = candidates[min(position, len(candidates) - 1)]
        return json.dumps({
            "subquestion": observation["subquestion"],
            "answer": observation["answer"],
        }, ensure_ascii=False)

    return execute


def _observed_evolution_action(trace: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    for event in reversed(trace):
        if event.get("event") != "skill_evolution":
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        return (
            str(result.get("action")) if result.get("action") else None,
            str(result.get("skill")) if result.get("skill") else None,
        )
    return None, None


def _teacher_feedback(phase: str, sample: dict[str, Any]) -> str:
    schemas = []
    for tool in sample.get("tools") or []:
        properties = (tool.get("input_schema") or {}).get("properties") or {}
        schemas.append({
            "api": tool.get("name"),
            "required_parameters": list((tool.get("input_schema") or {}).get("required") or []),
            "parameter_types": {
                name: spec.get("type") for name, spec in properties.items()
            },
        })
    schema_text = json.dumps(schemas, ensure_ascii=False, sort_keys=True)
    if phase == "add":
        return (
            "Teacher feedback: this is a durable, reusable workflow for this API task family. "
            "Create a Skill that requires the correct API and schema, without memorizing concrete "
            f"sample values. Gold API schema: {schema_text}"
        )
    if phase == "merge":
        return (
            "Teacher feedback: merge this durable clarification into the existing Skill for the "
            "same task family. Keep exact parameter names and types, but never store sample-specific "
            f"values or answers. Gold API schema: {schema_text}"
        )
    return (
        "Teacher feedback: this episode adds no durable lesson beyond the existing API schema. "
        "It is a one-off example, so discard it and do not modify or create any Skill."
    )


def _write_oracle_skill(family: str, sample: dict[str, Any]) -> str:
    from agents.skills import reset_skill_cache

    name = f"api-bank-{_slug(family)}"
    tool = (sample.get("tools") or [])[0]
    input_schema = tool.get("input_schema") or {}
    properties = input_schema.get("properties") or {}
    schema_lines = [
        f"- {key}: {value.get('type', 'string')} — {value.get('description', '')}"
        for key, value in properties.items()
    ]
    body = "\n".join([
        "---",
        f"name: {name}",
        f"description: Correctly handle {family} API-Bank tasks using the {tool.get('name')} API.",
        f"when-to-use: Use for requests in the {family} task family.",
        "user-invocable: false",
        "context: inline",
        "---",
        "",
        f"# {family} API workflow",
        "",
        f"Call {tool.get('name')} once all required information is known.",
        "Use the exact JSON parameter names and types below. Never invent missing values.",
        *schema_lines,
        "",
        "After the tool result, answer briefly. Do not memorize concrete benchmark values or answers.",
    ])
    path = Path.cwd() / ".bear" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + "\n", encoding="utf-8")
    reset_skill_cache()
    return name


def _row_from_agent(
    *,
    agent,
    result: dict[str, Any],
    sample: dict[str, Any],
    condition: str,
    phase: str,
    started: float,
    expected_skill_names: list[str],
    error: str | None = None,
) -> dict[str, Any]:
    trace = agent.get_trace()
    stop_reason = next((
        event.get("stop_reason") for event in reversed(trace)
        if event.get("event") == "chat_end"
    ), "error" if error else "unknown")
    return {
        "id": sample["id"],
        "condition": condition,
        "phase": phase,
        "family": sample.get("family"),
        "prediction": result.get("text", ""),
        "gold_calls": sample.get("gold_calls") or [],
        "expected_skill_names": expected_skill_names,
        "trace": trace,
        "tokens": agent.get_token_usage(),
        "latency_s": time.perf_counter() - started,
        "error": error,
        "stop_reason": stop_reason,
    }


async def _run_api_episode(
    job: dict[str, Any],
    sample: dict[str, Any],
    *,
    condition: str,
    phase: str,
    skill_mode: str,
    expected_skill_names: list[str],
    teacher_phase: str | None = None,
) -> dict[str, Any]:
    _reset_skill_cache()
    tools = list(sample.get("tools") or [])
    if skill_mode != "off":
        tools.extend(_skill_tool_definitions())
    agent = _agent_factory(
        job,
        tools=tools,
        custom_executor=_api_executor(sample),
        skill_mode=skill_mode,
        folding_mode="off",
    )
    started = time.perf_counter()
    try:
        result = await agent.run_once(sample["prompt"])
        if teacher_phase is not None:
            await agent.run_once(_teacher_feedback(teacher_phase, sample))
            await agent.drain_background_skill_tasks()
        row = _row_from_agent(
            agent=agent,
            result=result,
            sample=sample,
            condition=condition,
            phase=phase,
            started=started,
            expected_skill_names=expected_skill_names,
        )
    except Exception as error:
        row = _row_from_agent(
            agent=agent,
            result={"text": ""},
            sample=sample,
            condition=condition,
            phase=phase,
            started=started,
            expected_skill_names=expected_skill_names,
            error=f"{type(error).__name__}: {error}",
        )
    if teacher_phase is not None:
        action, skill_name = _observed_evolution_action(row["trace"])
        row["evolution_expected"] = teacher_phase
        row["evolution_observed"] = action
        row["evolved_skill_name"] = skill_name
    return row


async def run_skill_job(job: dict[str, Any]) -> list[dict[str, Any]]:
    from evals.datasets import load_skill_curriculum

    curriculum = load_skill_curriculum(job["preset"])
    condition = str(job["condition"])
    rows: list[dict[str, Any]] = []
    expected_by_family: dict[str, list[str]] = {}
    if condition == "oracle_static":
        for family, phases in curriculum["families"].items():
            expected_by_family[family] = [_write_oracle_skill(family, phases["add"])]
    elif condition == "evolved_skill":
        for family, phases in curriculum["families"].items():
            learned_names: list[str] = []
            for phase in ("add", "merge", "discard"):
                row = await _run_api_episode(
                    job,
                    phases[phase],
                    condition=condition,
                    phase=phase,
                    skill_mode="evolve",
                    expected_skill_names=list(learned_names),
                    teacher_phase=phase,
                )
                rows.append(row)
                if row.get("evolved_skill_name") and row["evolved_skill_name"] not in learned_names:
                    learned_names.append(row["evolved_skill_name"])
            expected_by_family[family] = learned_names

    skill_mode = "off" if condition == "skill_off" else "static"
    for family, phases in curriculum["families"].items():
        rows.append(await _run_api_episode(
            job,
            phases["heldout"],
            condition=condition,
            phase="heldout",
            skill_mode=skill_mode,
            expected_skill_names=expected_by_family.get(family, []),
        ))
    all_expected = sorted({name for names in expected_by_family.values() for name in names})
    rows.append(await _run_api_episode(
        job,
        curriculum["unrelated"],
        condition=condition,
        phase="unrelated",
        skill_mode=skill_mode,
        expected_skill_names=[],
    ))
    rows[-1]["available_skill_names"] = all_expected
    return rows


async def run_folding_job(job: dict[str, Any]) -> list[dict[str, Any]]:
    sample = job["sample"]
    condition = str(job["condition"])
    structured = condition == "folding_structured"
    tools = list(sample.get("tools") or [])
    if structured:
        tools.append(_compact_tool_definition())
    agent = _agent_factory(
        job,
        tools=tools,
        custom_executor=_toolhop_executor(sample),
        skill_mode="off",
        folding_mode="structured" if structured else "off",
        effective_window=6000 if structured else None,
        max_folds=1 if structured else 0,
    )
    started = time.perf_counter()
    error_text = None
    try:
        result = await agent.run_once(sample["prompt"])
    except Exception as error:
        result = {"text": ""}
        error_text = f"{type(error).__name__}: {error}"
    return [{
        "id": sample["id"],
        "condition": condition,
        "prediction": result.get("text", ""),
        "answer": sample["answer"],
        "subtasks": sample["subtasks"],
        "tools": sample["tools"],
        "trace": agent.get_trace(),
        "tokens": agent.get_token_usage(),
        "latency_s": time.perf_counter() - started,
        "error": error_text,
    }]


async def _judge_gaia(job: dict[str, Any], prediction: str, answer: str) -> tuple[bool | None, dict[str, Any]]:
    judge_model = job.get("judge_model")
    if not judge_model:
        return None, {}
    judge = _agent_factory(
        job,
        tools=[],
        custom_executor=None,
        skill_mode="off",
        folding_mode="off",
        model=str(judge_model),
    )
    side_query = judge._build_side_query(max_tokens=8)
    if side_query is None:
        return None, {}
    raw = await side_query(
        "Judge answer equivalence. Return only CORRECT or INCORRECT.",
        f"Reference answer: {answer}\nCandidate answer: {prediction}",
    )
    normalized = raw.strip().upper()
    return normalized.startswith("CORRECT"), judge.get_token_usage()


async def run_gaia_job(job: dict[str, Any]) -> list[dict[str, Any]]:
    sample = job["sample"]
    condition = str(job["condition"])
    structured = condition == "folding_structured"
    agent = _agent_factory(
        job,
        tools=None,
        custom_executor=None,
        skill_mode="off",
        folding_mode="structured" if structured else "off",
        effective_window=6000 if structured else None,
        max_folds=1 if structured else 0,
        mcp_enabled=bool(job.get("mcp_enabled")),
    )
    started = time.perf_counter()
    error_text = None
    try:
        result = await agent.run_once(sample["prompt"])
        judge_correct, judge_tokens = await _judge_gaia(
            job, result.get("text", ""), sample["answer"]
        )
    except Exception as error:
        result = {"text": ""}
        judge_correct, judge_tokens = None, {}
        error_text = f"{type(error).__name__}: {error}"
    return [{
        "id": sample["id"],
        "condition": condition,
        "prediction": result.get("text", ""),
        "answer": sample["answer"],
        "problem_type": sample.get("problem_type"),
        "attachment": sample.get("attachment"),
        "trace": agent.get_trace(),
        "tokens": agent.get_token_usage(),
        "judge_tokens": judge_tokens,
        "judge_correct": judge_correct,
        "latency_s": time.perf_counter() - started,
        "error": error_text,
    }]


async def run_job(job: dict[str, Any]) -> list[dict[str, Any]]:
    if job["suite"] == "skill":
        return await run_skill_job(job)
    if job["suite"] == "folding":
        return await run_folding_job(job)
    if job["suite"] == "gaia":
        return await run_gaia_job(job)
    raise ValueError(f"unsupported eval suite: {job['suite']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_file")
    args = parser.parse_args(argv)
    job_path = Path(args.job_file).resolve()
    job = json.loads(job_path.read_text(encoding="utf-8"))
    result_path = Path(job["result_path"]).resolve()
    repo_root = Path(job["repo_root"]).resolve()
    from dotenv import load_dotenv

    load_dotenv(repo_root / ".env", override=False)
    workspace = Path(job["workspace"]).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    os.chdir(workspace)
    try:
        rows = asyncio.run(run_job(job))
        _write_json(result_path, {"job_id": job["job_id"], "rows": rows})
        return 0
    except Exception as error:
        _write_json(result_path, {
            "job_id": job.get("job_id"),
            "rows": [],
            "error": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc(),
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
