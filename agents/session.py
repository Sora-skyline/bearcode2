#!/usr/bin/env python3
"""会话快照与结构化折叠记忆的轻量持久化层。

完整协议消息按 session id 保存到用户级目录，供 --resume 恢复；每次上下文折叠的
结构化记忆则以 JSONL 和 latest 快照写入项目 .bear/sessions，便于逐次审计。
Agent Runtime 负责决定保存时机，本模块只处理文件布局。

两类文件用途不同：``~/.bear-code/sessions`` 的完整协议消息用于 ``--resume``；项目内
``.bear/sessions`` 的 folding 记录用于观察压缩过程。后者不能单独还原完整对话，前者
也不适合作为精简后的模型上下文。
"""

from __future__ import annotations

from pathlib import Path

from typing import Any
import json

SESSION_DIR = Path.home() / ".bear-code" / "sessions"



def _ensure_dir() -> None:
    """按需创建用户级会话快照目录。"""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def get_project_session_dir() -> Path:
    """返回项目级折叠记忆目录，并确保目录已创建。"""
    d = Path.cwd() / ".bear" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(session_id: str, data: dict[str, Any]) -> None:
    """保存可恢复的完整会话快照；同一 session id 会覆盖旧快照。"""
    _ensure_dir()
    (SESSION_DIR / f"{session_id}.json").write_text(json.dumps(data, indent=2, default=str))


def save_folded_session_memory(session_id: str, record: dict[str, Any]) -> None:
    """追加一次折叠记录，同时更新便于读取的 latest JSON 快照。"""
    d = get_project_session_dir()
    line = json.dumps(record, ensure_ascii=False, default=str)
    with (d / f"{session_id}.folded-memory.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    (d / f"{session_id}.folded-memory.latest.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def load_session(session_id: str) -> dict[str, Any] | None:
    """读取指定会话；文件不存在或内容损坏时返回 None。"""
    path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def list_sessions() -> list[dict[str, Any]]:
    """扫描用户级快照，仅返回 REPL 列表和排序所需的 metadata。"""
    _ensure_dir()
    results = []
    for f in SESSION_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if "metadata" in data:
                results.append(data["metadata"])
        except Exception:
            pass
    return results


def get_latest_session_id() -> str | None:
    """按启动时间返回最近创建的 session id。"""
    sessions = list_sessions()
    if not sessions:
        return None
    sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    return sessions[0].get("id")
