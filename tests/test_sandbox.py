from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from agents.sandbox import (
    ExecRequest,
    ExecResult,
    SandboxConfig,
    SandboxError,
    SandboxSession,
    _command_summary,
)
from agents.mcp_client import McpConnection, McpManager


def test_sandbox_config_defaults(tmp_path: Path) -> None:
    config = SandboxConfig.from_mapping({}, workspace=tmp_path)
    assert config.backend == "docker"
    assert config.network == "none"
    assert config.memory == "1g"
    assert config.cpus == 1
    assert config.pids_limit == 128
    assert config.command_timeout_seconds == 120
    assert config.max_output_bytes == 1_048_576
    assert config.unsafe_local is False


def test_command_summary_does_not_log_inline_environment_secret() -> None:
    summary = _command_summary(ExecRequest(command="API_KEY=super-secret python task.py"))
    assert summary["executable"] == "python"
    assert "secret" not in str(summary)


def test_unstarted_session_close_does_not_emit_destroyed(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], dict[str, object]]:
        events: list[str] = []
        session = SandboxSession(
            SandboxConfig(workspace=tmp_path),
            event_sink=lambda event_type, **kwargs: events.append(event_type),
        )
        before = session.snapshot()
        await session.close()
        assert session.status == "closed"
        return events, before

    events, before = asyncio.run(scenario())
    assert before["status"] == "not-started"
    assert before["sandboxId"] is None
    assert events == []


def test_snapshot_reports_visible_workspace_secret_names_only(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("API_KEY=must-not-leak", encoding="utf-8")
    (tmp_path / ".env.local").write_text("TOKEN=must-not-leak", encoding="utf-8")
    (tmp_path / ".env.example").write_text("API_KEY=example", encoding="utf-8")
    (tmp_path / ".npmrc").write_text("token=must-not-leak", encoding="utf-8")

    snapshot = SandboxSession(SandboxConfig(workspace=tmp_path)).snapshot()

    assert snapshot["workspaceSecretFilesVisible"] == [".env", ".env.local", ".npmrc"]
    assert "must-not-leak" not in str(snapshot)


def test_docker_start_uses_hardening_flags(tmp_path: Path) -> None:
    async def scenario() -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []
        session = SandboxSession(SandboxConfig(workspace=tmp_path), session_id="test")

        async def fake_docker(*args: str, timeout: float = 30):
            calls.append(args)
            if args[0] == "create":
                return 0, "container-id", ""
            return 0, "", ""

        session._docker = fake_docker  # type: ignore[method-assign]
        await session.start()
        await session.close()
        return calls

    calls = asyncio.run(scenario())
    create = next(call for call in calls if call[0] == "create")
    joined = " ".join(create)
    assert "--network none" in joined
    assert "--init" in create
    assert "--read-only" in create
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--pids-limit 128" in joined
    assert "--volume" in create and ":/workspace:rw" in joined
    assert not any(".env" in part or "docker.sock" in part for part in create)
    assert calls[-1][:2] == ("rm", "-f")


def test_close_removes_created_container_even_if_marked_stopped(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[tuple[str, ...]], list[str]]:
        calls: list[tuple[str, ...]] = []
        events: list[str] = []
        session = SandboxSession(
            SandboxConfig(workspace=tmp_path),
            event_sink=lambda event_type, **kwargs: events.append(event_type),
        )

        async def fake_docker(*args: str, timeout: float = 30):
            calls.append(args)
            if args[0] == "create":
                return 0, "container-id", ""
            return 0, "", ""

        session._docker = fake_docker  # type: ignore[method-assign]
        await session.start()
        session._started = False
        assert session.status == "stopped"
        await session.close()
        return calls, events

    calls, events = asyncio.run(scenario())
    assert calls[-1][:2] == ("rm", "-f")
    assert events.count("sandbox.destroyed") == 1


def test_sandbox_image_installs_project_test_dependencies() -> None:
    dockerfile = (Path(__file__).parents[1] / "Dockerfile.sandbox").read_text(encoding="utf-8")
    assert "COPY requirements.txt" in dockerfile
    assert "-r /tmp/bear-code-requirements.txt" in dockerfile
    assert "pytest" in dockerfile


def test_missing_docker_fails_closed_without_local_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        session = SandboxSession(SandboxConfig(workspace=tmp_path))
        with pytest.raises(SandboxError, match="Docker CLI is unavailable"):
            await session.start()
        assert session.started is False

    monkeypatch.setenv("PATH", "")
    asyncio.run(scenario())


def test_unsafe_local_is_explicit_and_output_is_bounded(tmp_path: Path) -> None:
    async def scenario() -> ExecResult:
        config = SandboxConfig(
            unsafe_local=True,
            workspace=tmp_path,
            max_output_bytes=8,
        )
        session = SandboxSession(config)
        try:
            return await session.exec(ExecRequest(command="printf 1234567890"))
        finally:
            await session.close()

    result = asyncio.run(scenario())
    assert result.stdout == "12345678"
    assert result.output_truncated is True
    assert result.sandbox_id == "unsafe-local"


def test_timeout_returns_structured_result_and_restarts(tmp_path: Path) -> None:
    async def scenario() -> tuple[ExecResult, list[str]]:
        events: list[str] = []
        session = SandboxSession(
            SandboxConfig(unsafe_local=True, workspace=tmp_path),
            event_sink=lambda event_type, **kwargs: events.append(event_type),
        )
        try:
            result = await session.exec(
                ExecRequest(command="sleep 5", timeout_seconds=0.05)
            )
            return result, events
        finally:
            await session.close()

    result, events = asyncio.run(scenario())
    assert result.timed_out is True
    assert result.exit_code == 124
    assert "sandbox.restarted" in events


def test_mcp_project_config_requires_explicit_inclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".bear").mkdir(parents=True)
    project.mkdir()
    (home / ".bear" / "settings.json").write_text(
        '{"mcpServers":{"global":{"command":"global-server"}}}'
    )
    (project / ".mcp.json").write_text(
        '{"mcpServers":{"project":{"command":"project-server"}}}'
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    manager = McpManager()
    assert set(manager._load_configs(include_project=False)) == {"global"}
    assert set(manager._load_configs(include_project=True)) == {"global", "project"}
    assert manager.project_server_names() == ["project"]


def test_mcp_spawn_receives_only_explicit_environment(tmp_path: Path) -> None:
    class FakeProcess:
        def __init__(self):
            self.stdin = None
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.returncode = None

        def kill(self) -> None:
            self.returncode = -9

    class FakeSandbox:
        request: ExecRequest | None = None

        async def spawn_stdio(self, request: ExecRequest):
            self.request = request
            return FakeProcess()

    async def scenario() -> ExecRequest:
        sandbox = FakeSandbox()
        connection = McpConnection(
            "demo",
            "python",
            ["server.py"],
            {"EXPLICIT": "yes"},
            sandbox,  # type: ignore[arg-type]
        )
        await connection.connect()
        await asyncio.sleep(0)
        connection.close()
        assert sandbox.request is not None
        return sandbox.request

    request = asyncio.run(scenario())
    assert request.env == {"EXPLICIT": "yes"}
    assert request.command == "python"
    assert request.args == ("server.py",)


class _FakeSandbox:
    async def exec(self, request: ExecRequest) -> ExecResult:
        return ExecResult(
            execution_id="exec-1",
            stdout="ok\n",
            stderr="",
            exit_code=0,
            duration_ms=1,
            timed_out=False,
            cancelled=False,
            output_truncated=False,
            sandbox_id="fake",
        )


def test_docker_isolation_integration(tmp_path: Path) -> None:
    if os.environ.get("BEAR_RUN_DOCKER_TESTS") != "1":
        pytest.skip("set BEAR_RUN_DOCKER_TESTS=1 after building Dockerfile.sandbox")

    async def scenario() -> None:
        session = SandboxSession(SandboxConfig(workspace=tmp_path))
        try:
            outside = await session.exec(
                ExecRequest(command="test ! -e /var/run/docker.sock && test ! -e /host-home")
            )
            assert outside.exit_code == 0
            assert (await session.exec(ExecRequest(command="id -u"))).stdout.strip() != "0"
            network = await session.exec(
                ExecRequest(command="curl --connect-timeout 1 https://example.com", timeout_seconds=3)
            )
            assert network.exit_code != 0
            first = await session.exec(ExecRequest(command="touch /tmp/session-state"))
            second = await session.exec(ExecRequest(command="test -f /tmp/session-state"))
            assert first.exit_code == second.exit_code == 0
        finally:
            await session.close()

    asyncio.run(scenario())
