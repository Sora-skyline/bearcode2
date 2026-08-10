"""从真实对话和下一轮反馈中抽取、维护在线 Skill 候选。

本模块是“决策层”：Extractor 判断是否存在稳定可复用经验，Maintainer 决定 add、merge
或 discard，``online_ingest`` 编排权限与 provenance。文件写入、历史快照和统计由
``skill_evolution`` 负责。

完整链路分两次对话完成：本轮回答先进入 pending window，下一轮用户反馈补齐结果证据；
随后 Extractor 产出候选，Maintainer 与现有 Skill 比较，最后才可能写入。这个延迟窗口
避免仅凭模型自己的回答就把未经用户验证的做法沉淀成长期 Skill。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable


SideQuery = Callable[[str, str], Awaitable[str]]
ConfirmWrite = Callable[[str], Awaitable[bool]]


@dataclass
class OnlineSkillCandidate:
    """Extractor 返回的结构化候选；evidence 保留形成该规则的简短依据。"""
    name: str
    description: str
    when_to_use: str = ""
    instructions: str = ""
    evidence: str = ""
    tags: list[str] = field(default_factory=list)


def _parse_json_object(text: str) -> dict[str, Any]:
    """从 side query 的纯 JSON 或夹带说明文本中容错提取对象。"""
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except Exception:
            return {}
    return {}


def _normalize_identity(text: str) -> str:
    """规范化中英文名称文本，供候选与现有 Skill 做稳定身份比较。"""
    raw = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", raw).strip()


def _candidate_search_text(candidate: OnlineSkillCandidate) -> str:
    """拼接候选的可检索字段，用于相似 Skill 召回。"""
    return "\n".join(
        [
            candidate.name,
            candidate.description,
            candidate.when_to_use,
            candidate.instructions,
            " ".join(candidate.tags),
        ]
    )


def _coerce_candidate(obj: dict[str, Any]) -> OnlineSkillCandidate | None:
    """校验 Extractor 输出的最低字段，并转换为内部候选对象。"""
    name = str(obj.get("name") or "").strip()
    description = str(obj.get("description") or "").strip()
    instructions = str(obj.get("instructions") or obj.get("prompt") or "").strip()
    if not name or not description or not instructions:
        return None
    tags_raw = obj.get("tags") or []
    if isinstance(tags_raw, str):
        tags = [part.strip() for part in re.split(r"[,，]", tags_raw) if part.strip()]
    elif isinstance(tags_raw, list):
        tags = [str(part).strip() for part in tags_raw if str(part).strip()]
    else:
        tags = []
    return OnlineSkillCandidate(
        name=name,
        description=description,
        when_to_use=str(obj.get("when_to_use") or obj.get("when-to-use") or "").strip(),
        instructions=instructions,
        evidence=str(obj.get("evidence") or "").strip(),
        tags=tags[:8],
    )


async def extract_online_skill_candidate(
    *,
    messages: list[dict[str, Any]],
    side_query: SideQuery,
    retrieved_reference: dict[str, Any] | None = None,
    hint: str = "",
) -> OnlineSkillCandidate | None:
    """让 side query 从“任务、回答、下一轮反馈”中抽取耐久且可复用的 Skill。"""
    system = (
        "You are Bear Code's online Skill Extractor.\n"
        "Extract at most ONE reusable skill candidate from a live conversation window.\n"
        "Output ONLY strict JSON: {\"skills\": []} or {\"skills\": [{...}]}.\n\n"
        "Candidate fields: name, description, when_to_use, instructions, evidence, tags.\n\n"
        "Rules:\n"
        "- USER turns are the primary evidence. Assistant turns are context only.\n"
        "- A next user feedback turn may confirm, reject, or refine the prior assistant behavior.\n"
        "- Do not extract assistant-only guesses, weak confirmations, one-off task payload, secrets, project facts, URLs, account IDs, exact dates, or temporary parameters.\n"
        "- Extract only durable workflow, output policy, implementation preference, correction, or repeated constraint likely useful for future similar tasks.\n"
        "- Remove entity names and runtime-specific payload; use placeholders where needed.\n"
        "- retrieved_reference is identity context only; never treat it as new user evidence.\n"
        "- If evidence is weak, generic, or low-value, return {\"skills\": []}.\n"
    )
    payload = {
        # retrieved_reference 只帮助识别“是否在改已有 Skill”，不能替代真实用户证据。
        "messages": messages,
        "hint": hint,
        "retrieved_reference": retrieved_reference or None,
    }
    # 解析失败或空 skills 都按“没有可靠候选”处理，不让不稳定输出触发落盘。
    parsed = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    skills = parsed.get("skills")
    if not isinstance(skills, list) or not skills:
        return None
    first = skills[0]
    if not isinstance(first, dict):
        return None
    return _coerce_candidate(first)


def _exact_identity_match(candidate: OnlineSkillCandidate, skills: list[Any]) -> str:
    """用规范化名称/描述发现确定性同一 Skill，减少不必要的 LLM merge 判断。"""
    candidate_ids = {
        _normalize_identity(candidate.name),
        _normalize_identity(candidate.description),
        _normalize_identity(candidate.when_to_use),
    }
    candidate_ids.discard("")
    for skill in skills:
        skill_ids = {
            _normalize_identity(getattr(skill, "name", "")),
            _normalize_identity(getattr(skill, "description", "")),
            _normalize_identity(getattr(skill, "when_to_use", "") or ""),
        }
        skill_ids.discard("")
        if candidate_ids & skill_ids:
            return getattr(skill, "name", "")
    return ""


async def maintain_online_skill_candidate(
    *,
    candidate: OnlineSkillCandidate,
    side_query: SideQuery,
    retrieved_reference: dict[str, Any] | None = None,
    confirm_write: ConfirmWrite | None = None,
    target: str = "project",
) -> dict[str, Any]:
    """比较候选与现有 Skills，返回 add、merge 或 discard 的维护决策。"""
    from .skills import create_skill, discover_skills, evolve_skill, retrieve_relevant_skills

    # 先做确定性身份匹配，再把相似检索结果交给 Maintainer，降低重复 Skill 概率。
    skills = discover_skills()
    exact_target = _exact_identity_match(candidate, skills)
    similar_hits = retrieve_relevant_skills(_candidate_search_text(candidate), limit=8, min_score=0.03)
    top_reference_name = str((retrieved_reference or {}).get("name") or "").strip()

    system = (
        "You are Bear Code's online Skill Set Manager.\n"
        "Decide whether a candidate should add a new skill, merge into an existing skill, or be discarded.\n"
        "Output ONLY strict JSON.\n\n"
        "Schema:\n"
        "{\"action\":\"add|merge|discard\",\"target_skill\":\"existing name for merge\","
        "\"reason\":\"short reason\",\"merged_description\":\"optional\","
        "\"merged_when_to_use\":\"optional\",\"merged_instructions\":\"optional full merged SKILL.md body\"}\n\n"
        "Rules:\n"
        "- Prefer merge over add when the same capability already exists.\n"
        "- Discard if the candidate duplicates an existing shared/project skill and adds no user-specific durable improvement.\n"
        "- If merging, synthesize a complete merged instruction body, preserving useful existing guidance and adding only durable new guidance.\n"
        "- Do not preserve one-off payload, secrets, transient project facts, URLs, exact dates, or assistant-only claims.\n"
    )
    payload = {
        "candidate": asdict(candidate),
        "exact_identity_target": exact_target,
        "retrieved_reference": retrieved_reference or None,
        "similar_skills": similar_hits,
        "existing_skills": [
            {
                "name": getattr(skill, "name", ""),
                "description": getattr(skill, "description", ""),
                "when_to_use": getattr(skill, "when_to_use", "") or "",
                "source": getattr(skill, "source", ""),
                "context": getattr(skill, "context", ""),
                "instructions": (getattr(skill, "prompt_template", "") or "")[:6000],
            }
            for skill in skills[:80]
        ],
    }

    # LLM 只提出集合维护决策，下面仍会用确定性规则修正并经过权限确认。
    decision = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    action = str(decision.get("action") or "").strip().lower()
    target_skill = str(decision.get("target_skill") or "").strip()

    if exact_target:
        # 名称、描述或触发条件完全匹配时强制 merge，避免重复 add。
        action = "merge"
        target_skill = exact_target
    elif action == "add" and similar_hits:
        top = similar_hits[0]
        if float(top.get("score", 0.0)) >= 0.55:
            # 高相似候选即使 LLM 建议 add，也保守合并到最高分已有 Skill。
            action = "merge"
            target_skill = str(top.get("name") or "")
    elif action == "merge" and not target_skill:
        target_skill = top_reference_name

    if action not in {"add", "merge", "discard"}:
        # 非法或缺失动作默认 discard，保证异常模型输出不会触发写文件。
        action = "discard"

    if action == "discard":
        return {"ok": True, "action": "discard", "skill": "", "decision": decision}

    write_summary = f"online skill evolution: {action} {target_skill or candidate.name}"
    # Maintainer 只给建议；真正 add/merge 前仍需通过 Runtime 提供的写权限回调。
    if confirm_write is not None and not await confirm_write(write_summary):
        return {
            "ok": False,
            "action": f"{action}_denied",
            "skill": target_skill or candidate.name,
            "error": "permission denied",
            "decision": decision,
        }

    if action == "merge":
        target_skill = target_skill or top_reference_name
        if not target_skill:
            return {"ok": False, "action": "merge", "error": "missing target_skill", "decision": decision}
        # merge 委托持久化层保存旧快照并提升版本，不做无历史覆盖。
        result = evolve_skill(
            skill_name=target_skill,
            lesson=candidate.evidence or candidate.description,
            rationale=str(decision.get("reason") or "Online maintainer merge"),
            target="active",
            instructions=str(decision.get("merged_instructions") or candidate.instructions),
            description=str(decision.get("merged_description") or ""),
            when_to_use=str(decision.get("merged_when_to_use") or candidate.when_to_use),
            tags=candidate.tags,
        )
        return {"action": "merge", "candidate": asdict(candidate), "decision": decision, **result}

    # add 默认生成项目级、不可由用户斜杠直接调用的 inline Skill。
    result = create_skill(
        name=candidate.name,
        description=candidate.description,
        instructions=candidate.instructions,
        when_to_use=candidate.when_to_use,
        target=target,
        context="inline",
        user_invocable=False,
        evidence=candidate.evidence,
        actor="online",
        tags=candidate.tags,
    )
    return {"action": "add", "candidate": asdict(candidate), "decision": decision, **result}


async def online_ingest(
    *,
    messages: list[dict[str, Any]],
    side_query: SideQuery,
    retrieved_reference: dict[str, Any] | None = None,
    hint: str = "",
    confirm_write: ConfirmWrite | None = None,
    target: str = "project",
) -> dict[str, Any]:
    """在线自进化总入口：抽取候选、维护集合、请求写权限并记录全程审计。

    无论候选为空、被丢弃、拒绝、失败还是成功 add/merge，都会记录 provenance，确保
    后续 replay 评测能追溯 Skill 从哪段真实对话产生。
    """
    from .skills import record_online_provenance

    try:
        # 第一步只抽取结构化候选，Extractor 本身没有文件写权限。
        candidate = await extract_online_skill_candidate(
            messages=messages,
            side_query=side_query,
            retrieved_reference=retrieved_reference,
            hint=hint,
        )
    except Exception as exc:
        result = {"ok": False, "action": "failed", "error": str(exc)}
        record_online_provenance(
            action="failed",
            result=result,
            messages=messages,
            retrieved_reference=retrieved_reference,
            error=str(exc),
        )
        return result

    if candidate is None:
        # “没有值得沉淀的经验”是正常终态，也记录 none 供后续统计。
        result = {"ok": True, "action": "none"}
        record_online_provenance(
            action="none",
            result=result,
            messages=messages,
            retrieved_reference=retrieved_reference,
        )
        return result

    try:
        # 第二步结合现有集合和相似检索决定 add、merge 或 discard。
        result = await maintain_online_skill_candidate(
            candidate=candidate,
            side_query=side_query,
            retrieved_reference=retrieved_reference,
            confirm_write=confirm_write,
            target=target,
        )
    except Exception as exc:
        result = {"ok": False, "action": "failed", "skill": candidate.name, "error": str(exc)}

    # 所有终态统一记录真实消息、身份引用、维护决策和错误，作为 replay 来源。
    record_online_provenance(
        action=str(result.get("action") or "none"),
        skill_name=str(result.get("skill") or candidate.name),
        result=result,
        messages=messages,
        retrieved_reference=retrieved_reference,
        decision=result.get("decision") if isinstance(result.get("decision"), dict) else None,
        error="" if result.get("ok") else str(result.get("error") or ""),
    )
    return result


async def judge_retrieved_skill_usage(
    *,
    hits: list[dict[str, Any]],
    user_message: str,
    assistant_text: str,
    side_query: SideQuery | None = None,
) -> list[dict[str, Any]]:
    """逐个判断检索命中是否相关、回答是否实际使用，供 usage gate 累积统计。"""
    if not hits:
        return []
    if side_query is None:
        # 无 judge 时只能用名称是否出现在回答中的弱启发式，relevant 保守记为 False。
        assistant_lower = assistant_text.lower()
        return [
            {
                "name": hit.get("name", ""),
                "source": hit.get("source", ""),
                "skill_dir": hit.get("skill_dir", ""),
                "retrieved": True,
                "relevant": False,
                "used": str(hit.get("name", "")).lower() in assistant_lower,
                "score": float(hit.get("score", 0.0)),
                "reason": "heuristic fallback",
            }
            for hit in hits
        ]

    system = (
        "Judge whether retrieved skills were relevant to the user request and actually used in the assistant reply.\n"
        "Output ONLY strict JSON: {\"judgments\":[{\"name\":\"...\",\"relevant\":true|false,\"used\":true|false,\"reason\":\"short\"}]}.\n"
        "A skill is used only if the reply follows its distinctive workflow or policy, not merely because it was retrieved."
    )
    payload = {"user_message": user_message, "assistant_reply": assistant_text, "retrieved_skills": hits}
    parsed = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    # 按名称与原始 hits 对齐，保证每个 retrieved 候选都产生一条统计记录。
    raw_judgments = parsed.get("judgments") if isinstance(parsed.get("judgments"), list) else []
    by_name = {str(item.get("name") or ""): item for item in raw_judgments if isinstance(item, dict)}
    judgments: list[dict[str, Any]] = []
    for hit in hits:
        name = str(hit.get("name") or "")
        raw = by_name.get(name, {})
        judgments.append(
            {
                "name": name,
                "source": hit.get("source", ""),
                "skill_dir": hit.get("skill_dir", ""),
                "retrieved": True,
                "relevant": bool(raw.get("relevant")),
                "used": bool(raw.get("used")),
                "score": float(hit.get("score", 0.0)),
                "reason": str(raw.get("reason") or ""),
            }
        )
    return judgments
