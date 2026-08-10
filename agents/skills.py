"""Skill 的发现、检索、提示词展开、执行与维护入口。

Skill 是可复用的方法/流程，而不是事实记忆。用户级与项目级 ``SKILL.md`` 被解析为
``SkillDefinition``；检索只负责提示“可能相关”，显式调用才会返回完整 prompt 或 fork
配置。创建、演化和使用统计的实际落盘由 ``skill_evolution`` 完成。

需要区分三种动作：``retrieve_relevant_skills`` 只给模型相关摘要；``execute_skill`` 才
展开完整方法并记录显式调用；``create/evolve_skill`` 才修改磁盘。相关性命中不等于
模型实际采用，因此在线统计会另外记录 relevant 与 used。

从 Harness 视角看，Skill 不是一个新的基础模型，也不一定执行代码；它主要是一段经过
沉淀的操作说明。inline Skill 把说明加入主 Agent 上下文，fork Skill 则用独立子 Agent
执行说明，二者最终仍复用同一套“模型 -> 工具 -> 结果”循环。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .frontmatter import parse_frontmatter
from .skill_evolution import (
    create_skill_file,
    evolve_skill_file,
    format_skill_stats,
    record_online_skill_provenance,
    record_skill_feedback,
    record_skill_invocation,
    record_skill_usage_judgments,
)


@dataclass
class SkillDefinition:
    """一个 Skill 在 Runtime 内的统一表示，由 frontmatter 与 Markdown 正文组成。"""
    name: str
    description: str
    when_to_use: str | None = None
    allowed_tools: list[str] | None = None
    user_invocable: bool = True
    context: str = "inline"  # "inline" or "fork"
    prompt_template: str = ""
    source: str = "project"  # "project" or "user"
    skill_dir: str = ""


# skills 只在首次读取时扫描磁盘，后续复用缓存；修改 skill 后需要重启或 reset。
_cached_skills: list[SkillDefinition] | None = None


def execute_skill(skill_name:str, args:object)-> dict | None:
    """解析一次显式 Skill 调用并记录 invocation；真正的 fork 由 Agent 执行。"""
    # 这里只解析 Skill，不决定 inline/fork；执行方式由 Agent._execute_skill_tool 处理。
    skill = get_skill_by_name(skill_name)
    if not skill:
        return None

    # 每次显式调用都会进入 usage.jsonl，和“对话前自动检索”统计分开记录。
    record_skill_invocation(
        skill_name=skill.name,
        source=skill.source,
        context=skill.context,
        args=args,
    )

    return {
        "prompt": resolve_skill_prompt(skill, args),
        "allowed_tools": skill.allowed_tools,
        "context": skill.context,
        "source": skill.source,
        "skill_dir": skill.skill_dir,
    }



def resolve_skill_prompt(skill: SkillDefinition, args: object) -> str:
    """展开调用参数和 Skill 目录占位符，得到本次实际执行的 prompt。"""
    import re
    prompt = skill.prompt_template
    # 支持在 SKILL.md 正文中使用 $ARGUMENTS 或 ${ARGUMENTS} 引用用户参数。
    prompt = re.sub(r"\$ARGUMENTS|\$\{ARGUMENTS\}", str(args or ""), prompt)
    # 支持 skill 引用自己的目录，例如读取同目录下的 references/scripts。
    prompt = prompt.replace("${CLAUDE_SKILL_DIR}", skill.skill_dir)
    return prompt

def get_skill_by_name(skill_name:str)->SkillDefinition | None:
    """按 frontmatter name 精确查找已发现的 Skill。"""
    # 通过 name 查找 skill；name 来自 frontmatter，没有写时使用目录名。
    for s in discover_skills():
        if s.name == skill_name:
            return s
    return None

def discover_skills() -> list[SkillDefinition]:
    """扫描用户级和项目级 Skill 并缓存结果；同名时用户级定义优先。"""
    global _cached_skills
    if _cached_skills is not None:
        # Skill 创建、演化、归档后由对应入口主动 reset，普通请求复用进程内缓存。
        return _cached_skills

    skills: dict[str,SkillDefinition] = {}
    # 用户级 skills 优先级最高：~/.bear/skills/<name>/SKILL.md
    user_dir = Path.home() / ".bear" / "skills"
    _load_skills_from_dir(user_dir, "user", skills)
    # 项目级 skills 优先级较低：<cwd>/.bear/skills/<name>/SKILL.md
    project_dir = Path.cwd() / ".bear" / "skills"
    _load_skills_from_dir(project_dir, "project", skills, overwrite=False)

    _cached_skills = list(skills.values())
    return _cached_skills

def _load_skills_from_dir( base_dir: Path, source: str, skills:dict[str, SkillDefinition], overwrite: bool = True) -> None:
    """加载 ``<base>/<skill>/SKILL.md``，通过 overwrite 控制同名覆盖策略。"""
    # 只加载目录形式的 skill，不加载 .bear/skills/foo.md 这种单文件形式。
    if not base_dir.is_dir():
        return
    for entry in base_dir.iterdir():
        if not entry.is_dir():
            continue
        # 文件名必须是 SKILL.md，大小写要一致。
        skill_file = entry/  "SKILL.md"
        if not skill_file.exists():
            continue
        skill = _parse_skill_file(skill_file, source, str(entry))
        if skill:
            # 项目级加载时 overwrite=False，避免覆盖同名用户级 skill。
            if not overwrite and skill.name in skills:
                continue
            skills[skill.name] = skill

def _parse_skill_file(file_path: Path, source: str, skill_dir: str) -> SkillDefinition:
    """把 SKILL.md 的 frontmatter 和正文转换为 SkillDefinition。"""
    try:
        # SKILL.md = frontmatter 配置 + markdown 正文。
        raw = file_path.read_text()
        result = parse_frontmatter(raw)
        meta = result.meta

        # name 没写时用目录名；user-invocable 默认 true；context 默认 inline。
        name = meta.get("name") or file_path.parent.name or "unknown"
        user_invocable = meta.get("user-invocable", "true") != "false"
        context = "fork" if meta.get("context") == "fork" else "inline"

        allowed_tools: list[str] | None = None
        if "allowed-tools" in meta:
            raw_tools = meta["allowed-tools"]
            # allowed-tools 支持 JSON 数组字符串，也支持逗号分隔。
            if raw_tools.startswith("["):
                try:
                    allowed_tools = json.loads(raw_tools)
                except Exception:
                    allowed_tools = [s.strip() for s in raw_tools.strip("[]").split(",")]
            else:
                allowed_tools = [s.strip() for s in raw_tools.split(",")]

        return SkillDefinition(
            name=name,
            description=meta.get("description", ""),
            when_to_use=meta.get("when_to_use") or meta.get("when-to-use"),
            allowed_tools=allowed_tools,
            user_invocable=user_invocable,
            context=context,
            prompt_template=result.body,
            source=source,
            skill_dir=skill_dir,
        )

    except Exception:
        return None


def build_skill_descriptions() -> str:
    """生成 system prompt 中的轻量 Skill 清单，不展开完整正文。"""
    # 把已加载的 skills 写进 system prompt，让模型知道哪些 skill 可用。
    skills = discover_skills()

    lines = ["# Available Skills", ""]
    if not skills:
        lines.append("(No skills are currently registered.)")
        lines.append("")
    # user_invocable=True 的 skill 主要给用户通过 /<name> 手动调用。
    invocable = [s for s in skills if s.user_invocable]
    # user_invocable=False 的 skill 作为自动调用候选，模型根据 when_to_use 决定是否调用 skill 工具。
    auto_only = [s for s in skills if not s.user_invocable]

    if invocable:
        lines.append("User-invocable skills (user types /<name> to invoke):")
        for s in invocable:
            lines.append(f"- **/{s.name}**: {s.description}")
            if s.when_to_use:
                lines.append(f"  When to use: {s.when_to_use}")
        lines.append("")

    if auto_only:
        lines.append("Auto-invocable skills:")
        lines.append("When the user's request matches a skill's When to use, call the `skill` tool with that skill name before continuing. Do not ask the user to invoke it manually.")
        for s in auto_only:
            lines.append(f"- **{s.name}**: {s.description}")
            if s.when_to_use:
                lines.append(f"  When to use: {s.when_to_use}")
        lines.append("")

    lines.append("To invoke a skill programmatically, use the `skill` tool with the skill name and optional arguments.")
    lines.append("")
    lines.append("# Skill Evolution")
    lines.append("Bear Code has an online skill evolution loop after each assistant response. Do not create or evolve skills during normal task execution unless the user explicitly asks for manual skill maintenance.")
    lines.append("If manual maintenance is explicitly requested, call `skill_evolve` only for durable reusable feedback on an existing skill, and call `skill_create` only when no suitable existing skill exists.")
    lines.append("Never create or evolve skills from one-off task content, private secrets, temporary project facts, or assistant-only guesses.")
    return "\n".join(lines)


_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]{1,2}")
_STOP_TOKENS = {
    "请帮",
    "帮我",
    "我做",
    "做一",
    "一次",
    "一下",
    "这个",
    "那个",
    "一个",
    "用户",
    "问题",
    "回答",
    "生成",
    "使用",
    "需要",
}


def _tokens(text: str) -> set[str]:
    """生成去重词元，适合计算 query 与 Skill 的覆盖关系。"""
    raw = str(text or "").lower().replace("_", " ").replace("-", " ")
    found = {m.group(0) for m in _TOKEN_RE.finditer(raw)}
    expanded = set(found)
    for token in found:
        if len(token) > 3 and token.endswith("s"):
            expanded.add(token[:-1])
    found = expanded
    cjk = re.findall(r"[\u4e00-\u9fff]+", raw)
    for chunk in cjk:
        if len(chunk) >= 2:
            found.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return {x for x in found if x.strip() and x not in _STOP_TOKENS}


def _token_list(text: str) -> list[str]:
    """保留重复词元，供 BM25 风格的词频与文档长度计算。"""
    raw = str(text or "").lower().replace("_", " ").replace("-", " ")
    tokens = [m.group(0) for m in _TOKEN_RE.finditer(raw)]
    for chunk in re.findall(r"[\u4e00-\u9fff]+", raw):
        if len(chunk) >= 2:
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    expanded: list[str] = []
    for token in tokens:
        if not token.strip() or token in _STOP_TOKENS:
            continue
        expanded.append(token)
        if len(token) > 3 and token.endswith("s"):
            expanded.append(token[:-1])
    return expanded


def _skill_search_text(skill: SkillDefinition) -> str:
    """拼接名称、描述、触发条件和 prompt，形成统一检索语料。"""
    return "\n".join(
        [
            skill.name,
            skill.description,
            skill.when_to_use or "",
            skill.prompt_template[:4000],
        ]
    )


def retrieve_relevant_skills(
    query: str,
    *,
    limit: int = 3,
    min_score: float = 0.08,
) -> list[dict[str, Any]]:
    """使用轻量 BM25 风格词项匹配检索相关 Skill。

    元数据权重高于正文；中文补充二元切分，英文做基础词法归一化。结果只是候选，
    Runtime 仍要求模型根据用户意图决定是否采用。
    """
    query_terms = _token_list(query)
    # set 用于计算唯一词项交集；list 保留查询长度用于后续得分归一化。
    query_tokens = set(query_terms)
    if not query_tokens:
        return []

    docs: list[tuple[SkillDefinition, list[str]]] = []
    document_frequency: Counter[str] = Counter()
    for skill in discover_skills():
        # 元数据重复三次，相当于给名称、描述和触发条件高于正文的检索权重。
        meta_terms = _token_list("\n".join([skill.name, skill.description, skill.when_to_use or ""]))
        # 正文只取前 2500 字符，限制大型 SKILL.md 对检索成本和得分的影响。
        body_terms = _token_list(skill.prompt_template[:2500])
        terms = (meta_terms * 3) + body_terms
        if not terms:
            continue
        docs.append((skill, terms))
        document_frequency.update(set(terms))
    if not docs:
        return []

    avg_doc_len = sum(len(terms) for _, terms in docs) / max(1, len(docs))
    doc_count = len(docs)
    k1 = 1.4
    b = 0.75
    hits: list[dict[str, Any]] = []
    for skill, terms in docs:
        term_counts = Counter(terms)
        overlap = query_tokens & set(term_counts)
        if not overlap:
            continue
        raw_score = 0.0
        doc_len = max(1, len(terms))
        for token in overlap:
            # BM25：tf 表示文档词频，idf 提升稀有词，长度项抑制长正文偏置。
            tf = term_counts[token]
            idf = math.log(1 + (doc_count - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
            denom = tf + k1 * (1 - b + b * doc_len / max(1.0, avg_doc_len))
            raw_score += idf * (tf * (k1 + 1)) / max(denom, 0.0001)
        # 用户直接写出 Skill 名称时额外加分，但最终分数限制在 1.0。
        name_bonus = 0.15 if skill.name.lower() in str(query or "").lower() else 0.0
        score = min(1.0, (raw_score / max(3.0, len(query_tokens))) + name_bonus)
        if score < float(min_score):
            continue
        hits.append(
            {
                "score": float(score),
                "name": skill.name,
                "description": skill.description,
                "when_to_use": skill.when_to_use or "",
                "source": skill.source,
                "context": skill.context,
                "user_invocable": bool(skill.user_invocable),
                "skill_dir": skill.skill_dir,
            }
        )

    # 只返回超过阈值的前 limit 项，供 chat() 生成 <retrieved_skills> 注入块。
    hits.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    return hits[: max(1, int(limit or 1))]


def format_retrieved_skill_context(query: str, *, limit: int = 3) -> tuple[str, dict[str, Any] | None]:
    """将命中格式化为运行时注入块，并返回最高分引用及全部命中证据。"""
    hits = retrieve_relevant_skills(query, limit=limit)
    if not hits:
        return "", None
    lines = [
        "<retrieved_skills>",
        "These skills were retrieved for the current user request. Use a skill only if it directly matches the user's intent; otherwise ignore this block.",
    ]
    for idx, hit in enumerate(hits, start=1):
        lines.append(
            f"{idx}. {hit['name']} (score={float(hit['score']):.3f}, source={hit['source']}): {hit['description']}"
        )
        if hit.get("when_to_use"):
            lines.append(f"   When to use: {hit['when_to_use']}")
    lines.append("</retrieved_skills>")
    top = dict(hits[0])
    # top_ref 用于关联上一轮身份；all_hits 用于逐项记录 retrieved/relevant/used。
    top["all_hits"] = hits
    return "\n".join(lines), top


def reset_skill_cache() -> None:
    """清空发现缓存，使新建、演化或归档后的 Skill 可被立即重新加载。"""
    global _cached_skills
    _cached_skills = None


def evolve_skill(
    skill_name: str,
    lesson: str,
    rationale: str = "",
    target: str = "active",
    instructions: str = "",
    description: str = "",
    when_to_use: str = "",
    tags: list[str] | None = None,
) -> dict:
    """手动演化入口：解析目标 Skill 后委托持久化层保存快照和新版本。"""
    skill = get_skill_by_name(skill_name)
    result = evolve_skill_file(
        skill_name=skill_name,
        lesson=lesson,
        rationale=rationale,
        target=target,
        active_dir=skill.skill_dir if skill else "",
        instructions=instructions,
        description=description,
        when_to_use=when_to_use,
        tags=tags,
    )
    if result.get("ok"):
        # 落盘成功后丢弃发现缓存，Runtime 刷新 prompt 时会读到新版本。
        reset_skill_cache()
    return result


def create_skill(
    name: str,
    description: str,
    instructions: str,
    when_to_use: str = "",
    target: str = "project",
    context: str = "inline",
    user_invocable: bool = False,
    allowed_tools: object = None,
    evidence: str = "",
    actor: str = "agent",
    tags: list[str] | None = None,
) -> dict:
    """手动创建入口：封装 Skill 元数据后委托持久化层写入 SKILL.md。"""
    result = create_skill_file(
        name=name,
        description=description,
        instructions=instructions,
        when_to_use=when_to_use,
        target=target,
        context=context,
        user_invocable=user_invocable,
        allowed_tools=allowed_tools,
        evidence=evidence,
        actor=actor,
        tags=tags,
    )
    if result.get("ok"):
        # 新目录只有清缓存后才会被 discover_skills 纳入当前进程。
        reset_skill_cache()
    return result


def record_online_provenance(
    *,
    action: str,
    skill_name: str = "",
    result: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    retrieved_reference: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    """把在线 Extractor/Maintainer 的决定转发给持久化审计层。"""
    record_online_skill_provenance(
        action=action,
        skill_name=skill_name,
        result=result,
        messages=messages,
        retrieved_reference=retrieved_reference,
        decision=decision,
        error=error,
    )


def record_feedback(skill_name: str, rating: str, note: str = "") -> None:
    """记录用户对已存在 Skill 的显式评分与说明。"""
    record_skill_feedback(skill_name=skill_name, rating=rating, note=note)


def skill_stats() -> str:
    """返回适合 REPL 展示的 Skill 生命周期统计摘要。"""
    return format_skill_stats()


def record_usage_judgments(judgments: list[dict[str, Any]]) -> dict[str, Any]:
    """写入检索 Skill 的 relevant/used 判断，并在归档发生后刷新发现缓存。"""
    result = record_skill_usage_judgments(judgments)
    if result.get("pruned"):
        reset_skill_cache()
    return result
