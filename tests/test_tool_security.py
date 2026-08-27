from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import agents.tools as tools
from agents.sandbox import ExecRequest, ExecResult


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    memory = tmp_path / "memory"
    memory.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(tools, "get_memory_dir", lambda: memory)
    return root


def test_path_guard_rejects_absolute_parent_and_symlink_escape(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    for path in ("/etc/passwd", "../outside/secret.txt", "escape/secret.txt"):
        with pytest.raises(tools.ToolPathViolation):
            tools._resolve_tool_path(path)


def test_bypass_permissions_does_not_bypass_path_guard(workspace: Path) -> None:
    decision = tools.check_permission(
        "read_file", {"file_path": "/etc/passwd"}, mode="bypassPermissions"
    )
    assert decision["action"] == "deny"
    assert "Sandbox path violation" in decision["message"]


def test_plan_mode_blocks_shell_and_mcp(workspace: Path) -> None:
    assert tools.check_permission("run_shell", {"command": "pwd"}, mode="plan")["action"] == "deny"
    assert tools.check_permission("mcp__demo__write", {}, mode="plan")["action"] == "deny"


def test_shell_tool_requires_session(workspace: Path) -> None:
    result = asyncio.run(tools.execute_tool("run_shell", {"command": "pwd"}))
    assert "fail-closed" in result


def test_shell_tool_uses_injected_sandbox(workspace: Path) -> None:
    class FakeSandbox:
        async def exec(self, request: ExecRequest) -> ExecResult:
            assert request.command == "pwd"
            return ExecResult(
                execution_id="1",
                stdout="/workspace\n",
                stderr="",
                exit_code=0,
                duration_ms=1,
                timed_out=False,
                cancelled=False,
                output_truncated=False,
                sandbox_id="fake",
            )

    result = asyncio.run(
        tools.execute_tool(
            "run_shell",
            {"command": "pwd"},
            sandbox_session=FakeSandbox(),  # type: ignore[arg-type]
        )
    )
    assert result == "/workspace\n"
