"""Deterministic scorers for cached Bear Code evaluation predictions."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any, Iterable


INTERNAL_TOOLS = {
    "agent",
    "compact_context",
    "enter_plan_mode",
    "exit_plan_mode",
    "skill",
    "skill_create",
    "skill_evolve",
    "tool_search",
}


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def normalize_answer(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n.,;:!?'\"")


def extract_final_answer(text: str) -> str:
    text = str(text or "")
    boxed = re.findall(r"\\boxed\{([^{}]*)\}", text)
    if boxed:
        return boxed[-1].strip()
    marked = re.findall(r"(?im)^\s*(?:final\s+)?answer\s*:\s*(.+?)\s*$", text)
    if marked:
        return marked[-1].strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def answer_correct(prediction: str, gold: str) -> bool:
    return normalize_answer(extract_final_answer(prediction)) == normalize_answer(gold)


def _canonical_call(call: dict[str, Any]) -> str:
    name = str(call.get("name") or "")
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    return json.dumps([name, arguments], sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def api_call_metrics(
    predicted_calls: list[dict[str, Any]],
    gold_calls: list[dict[str, Any]],
) -> dict[str, float]:
    """Compare API names and JSON objects exactly while ignoring object key order."""
    predicted = Counter(_canonical_call(call) for call in predicted_calls)
    gold = Counter(_canonical_call(call) for call in gold_calls)
    matched = sum(min(count, gold.get(key, 0)) for key, count in predicted.items())
    denominator = max(len(predicted_calls), len(gold_calls), 1)
    return {
        "api_call_accuracy": matched / denominator,
        "task_success": float(predicted == gold),
        "matched_calls": float(matched),
        "predicted_calls": float(len(predicted_calls)),
        "gold_calls": float(len(gold_calls)),
    }


def tool_calls_from_trace(
    trace: list[dict[str, Any]],
    *,
    allowed_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for event in trace or []:
        if event.get("event") != "tool_call" or event.get("phase") != "start":
            continue
        name = str(event.get("name") or "")
        if allowed_names is not None:
            if name not in allowed_names:
                continue
        elif name in INTERNAL_TOOLS:
            continue
        calls.append({"name": name, "arguments": event.get("input") or {}})
    return calls


def _retrieval_ranks(row: dict[str, Any]) -> tuple[float, float]:
    expected = set(row.get("expected_skill_names") or [])
    if not expected:
        return 0.0, 0.0
    hits: list[dict[str, Any]] = []
    for event in row.get("trace") or []:
        if event.get("event") == "skill_retrieval":
            hits = list(event.get("hits") or [])
            break
    names = [
        str(hit.get("name") or hit.get("skill_name") or hit.get("skill") or "")
        for hit in hits[:3] if isinstance(hit, dict)
    ]
    rank = next((index + 1 for index, name in enumerate(names) if name in expected), None)
    return (1.0 if rank is not None else 0.0, 1.0 / rank if rank else 0.0)


def _skill_activated(row: dict[str, Any]) -> float:
    expected = set(row.get("expected_skill_names") or [])
    activations = [
        str(event.get("skill_name") or "")
        for event in row.get("trace") or []
        if event.get("event") == "skill_activation" and event.get("found")
    ]
    if not expected:
        return float(bool(activations))
    return float(any(name in expected for name in activations))


def _token_totals(rows: list[dict[str, Any]]) -> dict[str, float]:
    keys = ("input", "output", "side_input", "side_output", "total_input", "total_output")
    return {
        key: float(sum((row.get("tokens") or {}).get(key, 0) or 0 for row in rows))
        for key in keys
    }


def score_skill_predictions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sample_scores: list[dict[str, Any]] = []
    for row in rows:
        condition = str(row.get("condition") or "")
        by_condition[condition].append(row)
        allowed = {str(call.get("name") or "") for call in row.get("gold_calls") or []}
        predicted = tool_calls_from_trace(row.get("trace") or [], allowed_names=allowed)
        calls = api_call_metrics(predicted, row.get("gold_calls") or [])
        recall, reciprocal_rank = _retrieval_ranks(row)
        score = {
            "id": row.get("id"),
            "condition": condition,
            "phase": row.get("phase"),
            "family": row.get("family"),
            **calls,
            "recall_at_3": recall,
            "reciprocal_rank": reciprocal_rank,
            "skill_activated": _skill_activated(row),
            "error": row.get("error"),
        }
        sample_scores.append(score)

    summaries: dict[str, Any] = {}
    for condition, condition_rows in sorted(by_condition.items()):
        evaluation_rows = [
            row for row in condition_rows if row.get("phase") in {"heldout", "unrelated"}
        ]
        heldout_rows = [row for row in condition_rows if row.get("phase") == "heldout"]
        unrelated_rows = [row for row in condition_rows if row.get("phase") == "unrelated"]
        eval_scores = [
            score for score in sample_scores
            if score["condition"] == condition and score["phase"] in {"heldout", "unrelated"}
        ]
        heldout_scores = [score for score in eval_scores if score["phase"] == "heldout"]
        unrelated_scores = [score for score in eval_scores if score["phase"] == "unrelated"]
        evolution_rows = [
            row for row in condition_rows
            if row.get("evolution_expected") in {"add", "merge", "discard"}
        ]
        evolution_accuracy = _mean(
            float(row.get("evolution_observed") == row.get("evolution_expected"))
            for row in evolution_rows
        )
        summaries[condition] = {
            "samples": len(evaluation_rows),
            "task_success": _mean(score["task_success"] for score in eval_scores),
            "api_call_accuracy": _mean(score["api_call_accuracy"] for score in eval_scores),
            "heldout_task_success": _mean(score["task_success"] for score in heldout_scores),
            "recall_at_3": _mean(score["recall_at_3"] for score in heldout_scores),
            "mrr": _mean(score["reciprocal_rank"] for score in heldout_scores),
            "skill_activation_rate": _mean(score["skill_activated"] for score in heldout_scores),
            "evolution_action_accuracy": evolution_accuracy,
            "unrelated_task_success": _mean(score["task_success"] for score in unrelated_scores),
            "false_retrieval_rate": _mean(
                float(any(
                    event.get("event") == "skill_retrieval" and bool(event.get("hits"))
                    for event in row.get("trace") or []
                ))
                for row in unrelated_rows
            ),
            "false_activation_rate": _mean(_skill_activated(row) for row in unrelated_rows),
            "errors": sum(bool(row.get("error")) for row in evaluation_rows),
            "tokens": _token_totals(condition_rows),
        }

    baseline = {
        (score["id"], score["phase"]): score
        for score in sample_scores if score["condition"] == "skill_off"
    }
    paired: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for score in sample_scores:
        if score["condition"] == "skill_off" or score["phase"] not in {"heldout", "unrelated"}:
            continue
        base = baseline.get((score["id"], score["phase"]))
        if not base:
            continue
        paired[score["condition"]].append({
            "id": score["id"],
            "phase": score["phase"],
            "task_success_delta": score["task_success"] - base["task_success"],
            "api_call_accuracy_delta": score["api_call_accuracy"] - base["api_call_accuracy"],
        })

    return {
        "suite": "skill",
        "conditions": summaries,
        "paired_deltas": dict(paired),
        "samples": sample_scores,
    }


def _event_tool_outputs_before(trace: list[dict[str, Any]], stop_index: int) -> list[str]:
    return [
        str(event.get("output") or "")
        for event in trace[:stop_index]
        if event.get("event") == "tool_call" and event.get("phase") == "end" and not event.get("error")
    ]


def _folding_sample_metrics(row: dict[str, Any]) -> dict[str, Any]:
    trace = row.get("trace") or []
    allowed_names = {str(tool.get("name") or "") for tool in row.get("tools") or []}
    calls = tool_calls_from_trace(trace, allowed_names=allowed_names)
    call_names = [call["name"] for call in calls]
    duplicate_calls = sum(max(count - 1, 0) for count in Counter(call_names).values())
    outputs = [
        str(event.get("output") or "")
        for event in trace
        if event.get("event") == "tool_call"
        and event.get("phase") == "end"
        and event.get("name") in allowed_names
    ]
    output_blob = "\n".join(outputs).lower()
    subtask_answers = [str(value) for value in (row.get("subtasks") or {}).values()]
    path_hits = sum(normalize_answer(answer) in normalize_answer(output_blob) for answer in subtask_answers)
    fold_indices = [index for index, event in enumerate(trace) if event.get("event") == "fold"]
    fold_event = trace[fold_indices[0]] if fold_indices else None

    known_fact_recall = None
    hallucinated_tool_rate = None
    fallback = None
    compression_ratio = None
    if fold_event is not None:
        fold_index = fold_indices[0]
        prior_blob = "\n".join(_event_tool_outputs_before(trace, fold_index)).lower()
        observed_facts = [
            answer for answer in subtask_answers
            if normalize_answer(answer) and normalize_answer(answer) in normalize_answer(prior_blob)
        ]
        memory = fold_event.get("memory") if isinstance(fold_event.get("memory"), dict) else {}
        memory_blob = json.dumps(memory, ensure_ascii=False).lower()
        known_fact_recall = (
            _mean(float(normalize_answer(fact) in normalize_answer(memory_blob)) for fact in observed_facts)
            if observed_facts else 1.0
        )
        tools_used = ((memory.get("tool_memory") or {}).get("tools_used") or [])
        memory_tool_names = [
            str(item.get("tool_name") or "") for item in tools_used if isinstance(item, dict)
        ]
        hallucinated_tool_rate = (
            sum(name not in allowed_names for name in memory_tool_names) / len(memory_tool_names)
            if memory_tool_names else 0.0
        )
        fallback = float(bool(fold_event.get("fallback")))
        compression_ratio = float(fold_event.get("compression_ratio") or 0.0)

    structured = row.get("condition") == "folding_structured"
    return {
        "id": row.get("id"),
        "condition": row.get("condition"),
        "pass_at_1": float(answer_correct(str(row.get("prediction") or ""), str(row.get("answer") or ""))),
        "path_score": path_hits / max(len(subtask_answers), 1),
        "tool_calls": len(calls),
        "repeat_tool_call_rate": duplicate_calls / max(len(calls), 1),
        "fold_count": len(fold_indices),
        "invalid_intervention": bool(structured and not fold_indices),
        "known_fact_recall": known_fact_recall,
        "unknown_tool_hallucination_rate": hallucinated_tool_rate,
        "compression_ratio": compression_ratio,
        "fallback": fallback,
        "error": row.get("error"),
    }


def score_folding_predictions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sample_scores = [_folding_sample_metrics(row) for row in rows]
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for score in sample_scores:
        by_condition[str(score["condition"])].append(score)
    summaries: dict[str, Any] = {}
    for condition, scores in sorted(by_condition.items()):
        valid = [score for score in scores if not score["invalid_intervention"]]
        effect_scores = valid if condition == "folding_structured" else scores
        summaries[condition] = {
            "samples": len(scores),
            "valid_effect_samples": len(effect_scores),
            "invalid_interventions": sum(score["invalid_intervention"] for score in scores),
            "pass_at_1": _mean(score["pass_at_1"] for score in effect_scores),
            "path_score": _mean(score["path_score"] for score in effect_scores),
            "known_fact_recall": _mean(
                score["known_fact_recall"] for score in effect_scores
                if score["known_fact_recall"] is not None
            ),
            "unknown_tool_hallucination_rate": _mean(
                score["unknown_tool_hallucination_rate"] for score in effect_scores
                if score["unknown_tool_hallucination_rate"] is not None
            ),
            "compression_ratio": _mean(
                score["compression_ratio"] for score in effect_scores
                if score["compression_ratio"] is not None
            ),
            "repeat_tool_call_rate": _mean(score["repeat_tool_call_rate"] for score in effect_scores),
            "fallback_ratio": _mean(
                score["fallback"] for score in effect_scores if score["fallback"] is not None
            ),
            "errors": sum(bool(score["error"]) for score in scores),
            "tokens": _token_totals([
                row for row in rows if row.get("condition") == condition
            ]),
        }

    baseline = {
        score["id"]: score for score in sample_scores if score["condition"] == "folding_off"
    }
    paired: list[dict[str, Any]] = []
    for score in sample_scores:
        if score["condition"] != "folding_structured" or score["invalid_intervention"]:
            continue
        base = baseline.get(score["id"])
        if base:
            paired.append({
                "id": score["id"],
                "pass_at_1_delta": score["pass_at_1"] - base["pass_at_1"],
                "path_score_delta": score["path_score"] - base["path_score"],
                "repeat_tool_call_rate_delta": (
                    score["repeat_tool_call_rate"] - base["repeat_tool_call_rate"]
                ),
            })
    return {
        "suite": "folding",
        "conditions": summaries,
        "paired_deltas": paired,
        "samples": sample_scores,
    }


def score_gaia_predictions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    samples = []
    for row in rows:
        deterministic = answer_correct(str(row.get("prediction") or ""), str(row.get("answer") or ""))
        judged = row.get("judge_correct")
        samples.append({
            "id": row.get("id"),
            "condition": row.get("condition"),
            "pass_at_1": float(bool(judged) if judged is not None else deterministic),
            "deterministic_correct": float(deterministic),
            "error": row.get("error"),
        })
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        by_condition[str(sample["condition"])].append(sample)
    return {
        "suite": "gaia",
        "conditions": {
            condition: {
                "samples": len(condition_samples),
                "pass_at_1": _mean(item["pass_at_1"] for item in condition_samples),
                "errors": sum(bool(item["error"]) for item in condition_samples),
            }
            for condition, condition_samples in sorted(by_condition.items())
        },
        "samples": samples,
    }


def score_predictions(suite: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if suite == "skill":
        return score_skill_predictions(rows)
    if suite == "folding":
        return score_folding_predictions(rows)
    if suite == "gaia":
        return score_gaia_predictions(rows)
    raise ValueError(f"unsupported eval suite: {suite}")


def render_report(config: dict[str, Any], metrics: dict[str, Any]) -> str:
    """Render a compact report without making significance claims."""
    suite = str(metrics.get("suite") or config.get("suite") or "")
    lines = [
        f"# Bear Code Eval Report: {suite}",
        "",
        f"- Run ID: {config.get('run_id', '')}",
        f"- Model: {config.get('model', '')}",
        f"- Preset: {config.get('preset', '')}",
        f"- Git SHA: {config.get('git_sha', '')}",
        "- Statistical note: single low-cost run; raw paired differences only, no significance claim.",
        "",
        "## Condition summary",
        "",
    ]
    conditions = metrics.get("conditions") or {}
    if suite == "skill":
        lines.extend([
            "| Condition | Task success | API accuracy | Recall@3 | MRR | Activation | Evolution action |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ])
        for name, values in conditions.items():
            lines.append(
                f"| {name} | {_fmt(values.get('task_success'))} | {_fmt(values.get('api_call_accuracy'))} "
                f"| {_fmt(values.get('recall_at_3'))} | {_fmt(values.get('mrr'))} "
                f"| {_fmt(values.get('skill_activation_rate'))} | {_fmt(values.get('evolution_action_accuracy'))} |"
            )
    elif suite == "folding":
        lines.extend([
            "| Condition | Valid n | Pass@1 | Path | Fact recall | Hallucinated tools | Compression | Repeats | Fallback |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for name, values in conditions.items():
            lines.append(
                f"| {name} | {values.get('valid_effect_samples', 0)} | {_fmt(values.get('pass_at_1'))} "
                f"| {_fmt(values.get('path_score'))} | {_fmt(values.get('known_fact_recall'))} "
                f"| {_fmt(values.get('unknown_tool_hallucination_rate'))} | {_fmt(values.get('compression_ratio'))} "
                f"| {_fmt(values.get('repeat_tool_call_rate'))} | {_fmt(values.get('fallback_ratio'))} |"
            )
        invalid = (conditions.get("folding_structured") or {}).get("invalid_interventions", 0)
        lines.extend(["", f"Invalid structured interventions excluded from effect metrics: {invalid}."])
    else:
        lines.extend(["| Condition | Pass@1 | Errors |", "|---|---:|---:|"])
        for name, values in conditions.items():
            lines.append(f"| {name} | {_fmt(values.get('pass_at_1'))} | {values.get('errors', 0)} |")

    lines.extend(["", "## Paired differences", "", json.dumps(
        metrics.get("paired_deltas") or {}, ensure_ascii=False, indent=2
    ), "", "## Failed samples", ""])
    failures = [
        sample for sample in metrics.get("samples") or []
        if sample.get("error") or sample.get("task_success") == 0 or sample.get("pass_at_1") == 0
    ]
    if failures:
        for sample in failures[:10]:
            lines.append(
                f"- {sample.get('condition')} / {sample.get('id')}: "
                f"{sample.get('error') or 'metric failure'}"
            )
    else:
        lines.append("- None.")
    return "\n".join(lines).rstrip() + "\n"


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.3f}"
