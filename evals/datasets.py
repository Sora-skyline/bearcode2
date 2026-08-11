"""Dataset adapters used by the low-cost evaluation presets.

The adapters only parse local benchmark artifacts. In particular, ToolHop's
embedded Python functions are never imported or executed.
"""

from __future__ import annotations

import ast
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
API_BANK_SAMPLES = REPO_ROOT / "data" / "API-Bank" / "lv1-lv2-samples" / "level-1-given-desc-e2e"
API_BANK_APIS = REPO_ROOT / "data" / "API-Bank" / "apis"
TOOLHOP_PATH = REPO_ROOT / "data" / "ToolHop" / "ToolHop.json"
GAIA_PATH = REPO_ROOT / "data" / "GAIA" / "all.json"
MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"


def load_manifest(preset: str = "low-cost") -> dict[str, Any]:
    path = MANIFEST_DIR / f"{preset.replace('-', '_')}.json"
    if not path.is_file():
        raise ValueError(f"unknown eval preset: {preset}")
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSONL at {path}:{line_number}: {error}") from error
    return rows


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@lru_cache(maxsize=None)
def load_api_schema(api_name: str) -> dict[str, Any]:
    """Read an API-Bank class schema with AST literals, without importing it."""
    source_path = API_BANK_APIS / f"{_snake_case(api_name)}.py"
    if not source_path.is_file():
        raise FileNotFoundError(f"API-Bank schema not found: {source_path}")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    description = f"Call the {api_name} benchmark API."
    parameters: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != api_name:
            continue
        for statement in node.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            target_names = [target.id for target in targets if isinstance(target, ast.Name)]
            value = statement.value
            if "description" in target_names:
                description = str(ast.literal_eval(value))
            elif "input_parameters" in target_names:
                parameters = dict(ast.literal_eval(value))
        break
    if not parameters:
        raise ValueError(f"API-Bank schema has no input_parameters: {api_name}")

    type_map = {
        "str": "string",
        "string": "string",
        "int": "integer",
        "integer": "integer",
        "float": "number",
        "number": "number",
        "bool": "boolean",
        "boolean": "boolean",
        "list": "array",
        "dict": "object",
    }
    properties: dict[str, Any] = {}
    for name, spec in parameters.items():
        raw_type = str((spec or {}).get("type") or "string").lower()
        properties[name] = {
            "type": type_map.get(raw_type, "string"),
            "description": str((spec or {}).get("description") or ""),
        }
    return {
        "name": api_name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
        },
    }


def load_api_bank_sample(filename: str) -> dict[str, Any]:
    path = API_BANK_SAMPLES / filename
    if not path.is_file():
        raise FileNotFoundError(f"API-Bank sample not found: {path}")
    rows = _read_jsonl(path)
    first_api = next((index for index, row in enumerate(rows) if row.get("role") == "API"), len(rows))
    conversation = rows[:first_api]
    transcript = "\n".join(
        f"{row.get('role')}: {row.get('text', '')}" for row in conversation
        if row.get("role") in {"User", "AI"}
    )
    gold_calls: list[dict[str, Any]] = []
    for row in rows:
        if row.get("role") != "API":
            continue
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        arguments = result.get("input") if isinstance(result.get("input"), dict) else row.get("param_dict") or {}
        gold_calls.append({
            "name": str(row.get("api_name") or ""),
            "arguments": arguments,
            "result": row.get("result"),
        })
    api_names = list(dict.fromkeys(call["name"] for call in gold_calls if call["name"]))
    tools = [load_api_schema(name) for name in api_names]
    family = filename.split("-level-", 1)[0]
    return {
        "id": path.stem,
        "family": family,
        "source_path": str(path),
        "prompt": (
            "Continue this API-Bank conversation. Use the provided API tool with exact JSON "
            "parameters when enough information is available, then briefly answer the user.\n\n"
            f"{transcript}"
        ),
        "tools": tools,
        "gold_calls": gold_calls,
    }


def load_skill_curriculum(preset: str = "low-cost") -> dict[str, Any]:
    manifest = load_manifest(preset)["skill"]
    families: dict[str, dict[str, Any]] = {}
    for family, phases in manifest["families"].items():
        families[family] = {
            phase: load_api_bank_sample(filename)
            for phase, filename in phases.items()
        }
    return {
        "families": families,
        "unrelated": load_api_bank_sample(manifest["unrelated"]),
    }


def load_toolhop_samples(preset: str = "low-cost") -> list[dict[str, Any]]:
    manifest = load_manifest(preset)["folding"]
    rows = json.loads(TOOLHOP_PATH.read_text(encoding="utf-8"))
    by_id = {int(row["id"]): row for row in rows}
    selected: list[dict[str, Any]] = []
    for sample_id in manifest["ids"]:
        row = by_id[int(sample_id)]
        subtasks = row.get("sub_task") or {}
        if len(subtasks) < int(manifest["minimum_subtasks"]):
            raise ValueError(f"ToolHop sample {sample_id} does not meet the subtask threshold")
        tools_by_name: dict[str, dict[str, Any]] = {}
        tool_results: dict[str, list[dict[str, str]]] = {}
        for subquestion, answer in subtasks.items():
            spec = (row.get("tools") or {}).get(subquestion) or {}
            name = str(spec.get("name") or "")
            if not name:
                continue
            tools_by_name.setdefault(name, {
                "name": name,
                "description": str(spec.get("description") or ""),
                "input_schema": spec.get("parameters") or {"type": "object", "properties": {}},
            })
            tool_results.setdefault(name, []).append({
                "subquestion": str(subquestion),
                "answer": str(answer),
            })
        selected.append({
            "id": str(row["id"]),
            "question": str(row.get("question") or ""),
            "answer": str(row.get("answer") or ""),
            "subtasks": {str(key): str(value) for key, value in subtasks.items()},
            "tools": list(tools_by_name.values()),
            "tool_results": tool_results,
            "prompt": (
                "Solve this multi-hop question using the provided tools. Work through the evidence "
                "and finish with exactly 'ANSWER: <answer>'.\n\n"
                f"Question: {row.get('question', '')}"
            ),
        })
    return selected


def _find_gaia_attachment(file_name: str) -> str | None:
    if not file_name:
        return None
    attachment = REPO_ROOT / "data" / "GAIA" / "files" / file_name
    return str(attachment) if attachment.is_file() else None


def load_gaia_samples(preset: str = "low-cost") -> list[dict[str, Any]]:
    ids = {int(value) for value in load_manifest(preset)["gaia"]["ids"]}
    rows = json.loads(GAIA_PATH.read_text(encoding="utf-8"))
    selected: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: int(item["id"])):
        if int(row["id"]) not in ids:
            continue
        attachment = _find_gaia_attachment(str(row.get("file_name") or ""))
        prompt = str(row.get("Question") or "")
        if attachment:
            prompt += f"\n\nAttached local file: {attachment}"
        prompt += "\n\nFinish with exactly 'ANSWER: <answer>'."
        selected.append({
            "id": str(row["id"]),
            "question": str(row.get("Question") or ""),
            "answer": str(row.get("answer") or ""),
            "problem_type": str(row.get("problem_type") or "text"),
            "attachment": attachment,
            "prompt": prompt,
        })
    return selected
