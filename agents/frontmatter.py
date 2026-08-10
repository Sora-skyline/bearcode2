"""Memory、Skill 与自定义 Agent 共用的轻量 frontmatter 编解码器。

这里只支持 ``---`` 之间的简单 ``key: value``，不是完整 YAML 实现；保持依赖最小的
同时，也意味着列表和嵌套结构需要由上层自行解析。
"""

from dataclasses import dataclass, field


@dataclass
class FrontmatterResult:
    """解析后的元数据和去除 frontmatter 的 Markdown 正文。"""
    meta: dict[str, str] = field(default_factory=dict)
    body: str = ""


def parse_frontmatter(content: str) -> FrontmatterResult:
    """解析首个 frontmatter 区块；格式不完整时把原文全部视为正文。"""
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return FrontmatterResult(body=content)

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return FrontmatterResult(body=content)

    meta: dict[str, str] = {}
    for i in range(1, end_idx):
        colon_idx = lines[i].find(":")
        if colon_idx == -1:
            continue
        key = lines[i][:colon_idx].strip()
        value = lines[i][colon_idx + 1:].strip()
        if key:
            meta[key] = value

    body = "\n".join(lines[end_idx + 1:]).strip()
    return FrontmatterResult(meta=meta, body=body)


def format_frontmatter(meta: dict[str, str], body: str) -> str:
    """将扁平元数据与正文格式化为可落盘的 Markdown 文本。"""
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)
