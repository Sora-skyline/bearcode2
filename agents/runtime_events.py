"""Bear Code Runtime 的结构化事件、脱敏和 JSONL 持久化。

事件日志是 Web Console 的事实来源。发布失败只记 warning，不得反向打断 Agent Loop。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import threading
import uuid
from typing import Any

from .session import SESSION_DIR

SCHEMA_VERSION = "1.0"
MAX_EVENT_PAYLOAD_BYTES = 30 * 1024
_SENSITIVE_KEY = re.compile(
    r"(^|[_-])(api[_-]?key|authorization|token|secret|password|credential)([_-]|$)",
    re.IGNORECASE,
)


def _redact(value: Any, key: str = "") -> Any:
    collapsed_key = re.sub(r"[^a-z0-9]", "", key.lower())
    camel_sensitive = (
        collapsed_key in {"apikey", "authorization", "token", "secret", "password", "credential"}
        or collapsed_key.endswith(("accesstoken", "refreshtoken", "authtoken", "clientsecret", "clientpassword"))
    )
    if key and (_SENSITIVE_KEY.search(key) or camel_sensitive):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def sanitize_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """递归脱敏，并把超大 payload 改为可诊断的摘要。"""
    clean = _redact(payload or {})
    encoded = json.dumps(clean, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= MAX_EVENT_PAYLOAD_BYTES:
        return clean
    return {
        "truncated": True,
        "sizeBytes": len(encoded),
        "summary": encoded[:4096].decode("utf-8", errors="replace"),
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventJournal:
    """单个 Session 的追加日志和进程内实时订阅器。"""

    def __init__(self, session_id: str, directory: Path | None = None):
        self.session_id = session_id
        self.directory = directory or SESSION_DIR
        self.path = self.directory / f"{session_id}.events.jsonl"
        self._lock = threading.Lock()
        self._subscribers: set[asyncio.Queue] = set()
        self._seq = self._read_last_seq()

    def _read_last_seq(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        last = max(last, int(json.loads(line).get("seq", 0)))
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
        except OSError:
            return 0
        return last

    def publish(
        self,
        event_type: str,
        *,
        status: str = "completed",
        turn_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        agent_id: str = "main",
        duration_ms: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any]
        try:
            with self._lock:
                self._seq += 1
                event = {
                    "schemaVersion": SCHEMA_VERSION,
                    "id": uuid.uuid4().hex,
                    "seq": self._seq,
                    "timestamp": utc_now(),
                    "sessionId": self.session_id,
                    "turnId": turn_id,
                    "spanId": span_id or uuid.uuid4().hex[:16],
                    "parentSpanId": parent_span_id,
                    "agentId": agent_id,
                    "type": event_type,
                    "status": status,
                    "durationMs": duration_ms,
                    "payload": sanitize_payload(payload),
                }
                self.directory.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                    handle.flush()
        except Exception as exc:
            logging.warning("runtime event publish failed: %s", exc)
            return {}

        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except (asyncio.QueueFull, RuntimeError):
                continue
        return event

    def read(self, after: int = 0) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if not self.path.exists():
            return events
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if int(event.get("seq", 0)) > after:
                        events.append(event)
        except OSError:
            return []
        return events

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
