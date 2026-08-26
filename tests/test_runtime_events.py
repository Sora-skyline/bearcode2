from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

import agents.runtime_events as runtime_events
import agents.session as session_store
from agents.agent import Agent
from agents.runtime_events import EventJournal, sanitize_payload
from agents.web_runtime import WebRuntimeManager, create_web_app


def test_event_journal_redacts_and_replays_without_gaps(tmp_path: Path) -> None:
    journal = EventJournal("session", tmp_path)
    first = journal.publish(
        "turn.started",
        payload={"authorization": "Bearer secret", "accessToken": "secret", "tokenUsage": {"input": 3}, "safe": "ok"},
    )
    second = journal.publish("turn.completed", payload={"nested": {"api_key": "secret"}})

    assert first["seq"] == 1
    assert second["seq"] == 2
    assert first["payload"]["authorization"] == "[REDACTED]"
    assert first["payload"]["accessToken"] == "[REDACTED]"
    assert first["payload"]["tokenUsage"] == {"input": 3}
    assert journal.read(after=1) == [second]
    assert "secret" not in journal.path.read_text(encoding="utf-8")


def test_large_payload_is_summarized() -> None:
    payload = sanitize_payload({"result": "x" * (31 * 1024)})
    assert payload["truncated"] is True
    assert payload["sizeBytes"] > 30 * 1024


def _manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WebRuntimeManager:
    monkeypatch.setattr(runtime_events, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(session_store, "SESSION_DIR", tmp_path)
    return WebRuntimeManager(
        model="test-model",
        api_base="http://127.0.0.1:9/v1",
        api_key="must-not-leak",
        use_openai=True,
    )


def test_web_api_hides_credentials_and_changes_idle_permission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    with TestClient(create_web_app(manager)) as client:
        config = client.get("/api/config")
        created = client.post("/api/sessions", json={})
        session_id = created.json()["id"]
        resumed = client.post(f"/api/sessions/{session_id}/resume")
        wrong_method = client.get(f"/api/sessions/{session_id}/resume")
        changed = client.patch(f"/api/sessions/{session_id}", json={"permissionMode": "plan"})
        root = client.get("/")

    assert config.status_code == 200
    assert "must-not-leak" not in config.text
    assert created.status_code == 201
    assert resumed.status_code == 200
    assert created.json()["lastEventSeq"] == 1
    assert resumed.json()["lastEventSeq"] == 1
    assert wrong_method.status_code == 404
    assert changed.status_code == 200
    assert changed.json()["permissionMode"] == "plan"
    assert root.headers["cache-control"] == "no-cache"


def test_manager_allows_only_one_active_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, monkeypatch)
        first = manager.create_session()
        second = manager.create_session()
        release = asyncio.Event()

        async def slow_chat(_content: str, *, turn_id: str | None = None) -> None:
            await release.wait()

        monkeypatch.setattr(first.agent, "chat", slow_chat)
        await manager.send_message(first.id, "first")
        with pytest.raises(HTTPException) as captured:
            await manager.send_message(second.id, "second")
        assert captured.value.status_code == 409
        release.set()
        assert first.task is not None
        await first.task
        assert manager.config()["runtimeStatus"] == "idle"

    asyncio.run(scenario())


def test_plan_approval_receives_file_content(tmp_path: Path) -> None:
    async def scenario() -> None:
        agent = Agent(model="test", api_key="dummy", permission_mode="plan")
        plan = tmp_path / "plan.md"
        plan.write_text("# Approved plan\n\nImplement the console.", encoding="utf-8")
        agent._plan_file_path = str(plan)
        received: list[str] = []

        async def approve(content: str) -> dict[str, str]:
            received.append(content)
            return {"choice": "manual-execute"}

        agent.set_plan_approval_fn(approve)
        await agent._execute_plan_mode_tool("exit_plan_mode")
        assert received == ["# Approved plan\n\nImplement the console."]

    asyncio.run(scenario())
