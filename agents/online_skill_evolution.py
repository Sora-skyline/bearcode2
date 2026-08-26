"""从真实对话和下一轮反馈中抽取、维护在线 Skill 候选。

本模块是“决策层”：Attributor / Extractor 先隔离不可修复反馈并提炼稳定经验，Maintainer
决定 add、merge 或 discard，``online_ingest`` 编排 proposal 与 provenance。文件写入、历史快照和统计由
``skill_evolution`` 负责。

完整链路分两次对话完成：本轮回答先进入 pending window，下一轮用户反馈补齐结果证据；
随后可修复性归因决定是否产出候选，Maintainer 与现有 Skill 比较，最后只写隔离 proposal。这个延迟窗口
避免仅凭模型自己的回答就把未经用户验证的做法沉淀成长期 Skill。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable


SideQuery = Callable[[str, str], Awaitable[str]]


@dataclass
class OnlineSkillCandidate:
    """Extractor 返回的结构化候选；evidence 保留形成该规则的简短依据。"""
    name: str
    description: str
    when_to_use: str = ""
    instructions: str = ""
    evidence: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class OnlineFeedbackAnalysis:
    """反馈的可修复性归因；只有 skill_gap 可以进入 Skill 维护链路。"""

    root_cause: str
    reason: str = ""
    evidence: str = ""
    candidate: OnlineSkillCandidate | None = None


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


def _coerce_feedback_analysis(obj: dict[str, Any]) -> OnlineFeedbackAnalysis:
    """校验联合归因/抽取输出；异常结果保守归为 evaluation_noise。"""
    allowed = {"skill_gap", "capability_limit", "evaluation_noise"}
    root_cause = str(obj.get("root_cause") or "").strip().lower()
    if root_cause not in allowed:
        root_cause = "evaluation_noise"

    candidate = None
    skills = obj.get("skills")
    if root_cause == "skill_gap" and isinstance(skills, list) and skills and isinstance(skills[0], dict):
        candidate = _coerce_candidate(skills[0])

    return OnlineFeedbackAnalysis(
        root_cause=root_cause,
        reason=str(obj.get("reason") or "").strip(),
        evidence=str(obj.get("evidence") or "").strip(),
        candidate=candidate,
    )


def _attribution_payload(analysis: OnlineFeedbackAnalysis) -> dict[str, str]:
    """返回适合 provenance/report 的紧凑归因，不重复保存候选正文。"""
    return {
        "root_cause": analysis.root_cause,
        "reason": analysis.reason,
        "evidence": analysis.evidence,
    }


async def analyze_online_feedback(
    *,
    messages: list[dict[str, Any]],
    side_query: SideQuery,
    skill_trace: dict[str, Any] | None = None,
    hint: str = "",
) -> OnlineFeedbackAnalysis:
    """联合完成反馈归因和候选抽取，避免不可修复失败污染 Skill。"""
    system = (
        "You are Bear Code's online Skill Feedback Attributor and Extractor.\n"
        "First classify whether the observed feedback is repairable by changing reusable Skill guidance.\n"
        "Then extract at most ONE candidate only for a skill_gap.\n"
        "Output ONLY strict JSON.\n\n"
        "Schema:\n"
        '{"root_cause":"skill_gap|capability_limit|evaluation_noise",'
        '"reason":"short reason","evidence":"short user-grounded evidence","skills":[]}\n\n'
        "Root-cause rules:\n"
        "- skill_gap: missing or incorrect durable workflow, policy, implementation preference, or output constraint that changing a Skill can fix.\n"
        "- capability_limit: missing tool, permission, network, sandbox, runtime, model capability, or infrastructure support; Skill text cannot fix it.\n"
        "- evaluation_noise: no clear failure or durable correction, contradictory/ambiguous feedback, or insufficient user evidence.\n\n"
        "Candidate fields: name, description, when_to_use, instructions, evidence, tags.\n\n"
        "Extraction rules:\n"
        "- USER turns are the primary evidence. Assistant turns are context only.\n"
        "- A next user feedback turn may confirm, reject, or refine the prior assistant behavior.\n"
        "- For capability_limit or evaluation_noise, skills MUST be [].\n"
        "- Do not extract assistant-only guesses, weak confirmations, one-off task payload, secrets, project facts, URLs, account IDs, exact dates, or temporary parameters.\n"
        "- Extract only durable guidance likely useful for future similar tasks.\n"
        "- Remove entity names and runtime-specific payload; use placeholders where needed.\n"
        "- skill_trace is identity context only; never treat retrieved, surfaced, or invoked metadata as new user evidence.\n"
        "- If evidence is weak, generic, or low-value, classify evaluation_noise and return skills=[].\n"
    )
    payload = {
        "messages": messages,
        "hint": hint,
        "skill_trace": skill_trace or None,
    }
    parsed = _parse_json_object(await side_query(system, json.dumps(payload, ensure_ascii=False)))
    return _coerce_feedback_analysis(parsed)


async def extract_online_skill_candidate(
    *,
    messages: list[dict[str, Any]],
    side_query: SideQuery,
    skill_trace: dict[str, Any] | None = None,
    hint: str = "",
) -> OnlineSkillCandidate | None:
    """兼容旧调用方：归因完成后只返回可修复的 Skill 候选。"""
    analysis = await analyze_online_feedback(
        messages=messages,
        side_query=side_query,
        skill_trace=skill_trace,
        hint=hint,
    )
    return analysis.candidate


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
    skill_trace: dict[str, Any] | None = None,
    target: str = "project",
    attribution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """比较候选与现有 Skills，并把 add/merge 决策保存为待评测 proposal。"""
    from .skill_evolution import stage_skill_proposal
    from .skills import discover_skills, retrieve_relevant_skills

    # 先做确定性身份匹配，再把相似检索结果交给 Maintainer，降低重复 Skill 概率。
    skills = discover_skills() # 加载现有 Skills
    skills_by_name = {str(getattr(skill, "name", "") or "").strip(): skill for skill in skills}
    exact_target = _exact_identity_match(candidate, skills)# 精确身份匹配
    similar_hits = retrieve_relevant_skills(_candidate_search_text(candidate), limit=8, min_score=0.03)# 相似 Skill 检索
    existing_names = {str(getattr(skill, "name", "") or "").strip() for skill in skills}
    invoked_names = []
    for item in list((skill_trace or {}).get("invoked") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if name and name in existing_names and name not in invoked_names:
            invoked_names.append(name)

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
        "- invoked skills are strong identity context; retrieved and surfaced skills are audit context only and cannot decide a merge target.\n"
        "- Do not preserve one-off payload, secrets, transient project facts, URLs, exact dates, or assistant-only claims.\n"
    )
    payload = {
        "candidate": asdict(candidate),
        "exact_identity_target": exact_target,
        "skill_trace": skill_trace or None,
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

    # LLM 只提出集合维护决策，下面仍会用确定性规则修正并保存为隔离 proposal。
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
    elif action == "merge":
        # LLM 给出的目标必须真实存在；检索 Top 1 不能作为隐式目标。
        if target_skill not in existing_names:
            target_skill = ""
        if not target_skill and len(invoked_names) == 1:
            target_skill = invoked_names[0]
        if not target_skill and similar_hits:
            top = similar_hits[0]
            top_name = str(top.get("name") or "")
            if float(top.get("score", 0.0)) >= 0.55 and top_name in existing_names:
                target_skill = top_name
        if not target_skill:
            action = "discard"

    if action not in {"add", "merge", "discard"}:
        # 非法或缺失动作默认 discard，保证异常模型输出不会触发写文件。
        action = "discard"

    if action == "discard":
        return {"ok": True, "action": "discard", "skill": "", "decision": decision}

    # Proposal 只是隔离评测产物，不改变 Runtime 行为；写权限留到显式 publish 阶段。
    if action == "merge":
        if not target_skill:
            return {"ok": False, "action": "merge", "error": "missing target_skill", "decision": decision}
        existing = skills_by_name[target_skill]
        existing_instructions = str(getattr(existing, "prompt_template", "") or "").strip()
        merged_instructions = str(decision.get("merged_instructions") or "").strip()
        if not merged_instructions:
            addition = candidate.instructions.strip()
            merged_instructions = existing_instructions
            if addition and addition not in existing_instructions:
                merged_instructions = (existing_instructions + "\n\n## Proposed Addition\n\n" + addition).strip()
        snapshot = {
            "name": target_skill,
            "description": str(decision.get("merged_description") or getattr(existing, "description", "") or ""),
            "when_to_use": str(decision.get("merged_when_to_use") or getattr(existing, "when_to_use", "") or ""),
            "instructions": merged_instructions,
            "context": str(getattr(existing, "context", "inline") or "inline"),
            "user_invocable": bool(getattr(existing, "user_invocable", False)),
            "source": str(getattr(existing, "source", "project") or "project"),
            "tags": candidate.tags,
        }
        result = stage_skill_proposal(
            skill_name=target_skill,
            requested_action="merge",
            snapshot=snapshot,
            target=snapshot["source"],
            evidence=candidate.evidence or candidate.description,
            rationale=str(decision.get("reason") or "Online maintainer merge proposal"),
            attribution=attribution,
        )
        return {"requested_action": "merge", "candidate": asdict(candidate), "decision": decision, **result}

    # add proposal 默认面向项目级、不可由用户斜杠直接调用的 inline Skill。
    result = stage_skill_proposal(
        skill_name=candidate.name,
        requested_action="add",
        snapshot={
            "name": candidate.name,
            "description": candidate.description,
            "instructions": candidate.instructions,
            "when_to_use": candidate.when_to_use,
            "context": "inline",
            "user_invocable": False,
            "source": target,
            "tags": candidate.tags,
        },
        target=target,
        evidence=candidate.evidence,
        rationale=str(decision.get("reason") or "Online maintainer add proposal"),
        attribution=attribution,
    )
    return {"requested_action": "add", "candidate": asdict(candidate), "decision": decision, **result}


async def online_ingest(
    *,
    messages: list[dict[str, Any]],
    side_query: SideQuery,
    skill_trace: dict[str, Any] | None = None,
    hint: str = "",
    target: str = "project",
) -> dict[str, Any]:
    """在线自进化总入口：抽取候选、维护集合、暂存 proposal 并记录全程审计。

    无论候选为空、被丢弃、拒绝、失败还是成功 add/merge，都会记录 provenance，确保
    后续 replay 评测能追溯 Skill 从哪段真实对话产生。
    """
    from .skills import record_online_provenance

    try:
        # 归因与抽取在一次隔离请求中完成；不可修复失败不会进入 Maintainer。
        analysis = await analyze_online_feedback(
            messages=messages,
            side_query=side_query,
            skill_trace=skill_trace,
            hint=hint,
        )
    except Exception as exc:
        result = {"ok": False, "action": "failed", "error": str(exc)}
        record_online_provenance(
            action="failed",
            result=result,
            messages=messages,
            skill_trace=skill_trace,
            error=str(exc),
        )
        return result

    attribution = _attribution_payload(analysis)
    candidate = analysis.candidate
    if candidate is None:
        # 能力限制、评测噪声或没有结构化候选都是正常终态，并保留可审计归因。
        result = {"ok": True, "action": "none", "attribution": attribution}
        record_online_provenance(
            action="none",
            result=result,
            messages=messages,
            skill_trace=skill_trace,
        )
        return result

    try:
        # 第二步结合现有集合和相似检索决定 add、merge 或 discard。
        result = await maintain_online_skill_candidate(
            candidate=candidate,
            side_query=side_query,
            skill_trace=skill_trace,
            target=target,
            attribution=attribution,
        )
    except Exception as exc:
        result = {"ok": False, "action": "failed", "skill": candidate.name, "error": str(exc)}

    result["attribution"] = attribution

    # 所有终态统一记录真实消息、身份引用、维护决策和错误，作为 replay 来源。
    record_online_provenance(
        action=str(result.get("action") or "none"),
        skill_name=str(result.get("skill") or candidate.name),
        result=result,
        messages=messages,
        skill_trace=skill_trace,
        decision=result.get("decision") if isinstance(result.get("decision"), dict) else None,
        error="" if result.get("ok") else str(result.get("error") or ""),
    )
    return result

# 让一个额外的 LLM 在回答完成后做“行为判断”。
async def judge_retrieved_skill_adoption(
    *,
    hits: list[dict[str, Any]],
    user_message: str,
    assistant_text: str,
    surfaced_names: set[str] | None = None,
    invoked_names: set[str] | None = None,
    side_query: SideQuery | None = None,
) -> list[dict[str, Any]]:
    """逐个判断检索命中是否相关、回答是否看似采用，确定性调用由 Runtime 提供。"""
    if not hits:
        return []
    surfaced = set(surfaced_names or set())
    invoked = set(invoked_names or set())
    if side_query is None:
        # 无 judge 时不再用名称命中冒充使用证据，两个语义推测都保守记为 False。
        return [
            {
                "name": hit.get("name", ""),
                "source": hit.get("source", ""),
                "skill_dir": hit.get("skill_dir", ""),
                "retrieved": True,
                "surfaced": str(hit.get("name") or "") in surfaced,
                "invoked": str(hit.get("name") or "") in invoked,
                "relevant": False,
                "inferred_used": False,
                "score": float(hit.get("score", 0.0)),
                "reason": "judge unavailable",
            }
            for hit in hits
        ]

    system = (
        "Judge whether retrieved skills were relevant and whether the reply appears to adopt their distinctive guidance.\n"
        "Output ONLY strict JSON: {\"judgments\":[{\"name\":\"...\",\"relevant\":true|false,\"inferred_used\":true|false,\"reason\":\"short\"}]}.\n"
        "inferred_used means the reply appears to follow the skill's distinctive workflow; it never proves a real invocation."
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
                "surfaced": name in surfaced,
                "invoked": name in invoked,
                "relevant": bool(raw.get("relevant")),
                "inferred_used": bool(raw.get("inferred_used")),
                "score": float(hit.get("score", 0.0)),
                "reason": str(raw.get("reason") or ""),
            }
        )
    return judgments
