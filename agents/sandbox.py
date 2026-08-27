"""Bear Code 的轻量执行 Sandbox。

Approval 决定一项动作是否需要用户同意，本模块只负责动作获准后的技术隔离。默认后端
通过 Docker CLI 管理一个会话级持久容器；``unsafe_local`` 是唯一允许宿主执行的显式
兼容开关，Docker 失败时不会自动回退。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import time
from typing import Any, Callable
import uuid


DEFAULT_SANDBOX_IMAGE = "bear-code-sandbox:latest"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORKSPACE_SECRET_NAMES = {".npmrc", ".pypirc"}
_WORKSPACE_SECRET_TEMPLATE_SUFFIXES = (".example", ".sample", ".template")


class SandboxError(RuntimeError):
    """Sandbox 无法安全启动或执行时的 fail-closed 错误。"""


@dataclass(frozen=True)
class SandboxConfig:
    """会话级 Sandbox 配置；字段与项目 settings.json 的 camelCase 键对应。"""

    backend: str = "docker"
    image: str = DEFAULT_SANDBOX_IMAGE
    network: str = "none"
    memory: str = "1g"
    cpus: float = 1.0
    pids_limit: int = 128
    command_timeout_seconds: float = 120.0
    max_output_bytes: int = 1_048_576
    unsafe_local: bool = False
    workspace: Path = field(default_factory=Path.cwd)

    @classmethod
    def from_mapping(
        cls,
        raw: dict[str, Any] | None,
        *,
        workspace: Path | None = None,
        unsafe_local: bool = False,
    ) -> "SandboxConfig":
        values = raw or {}
        backend = str(values.get("backend", "docker"))
        if backend != "docker":
            raise ValueError("sandbox.backend currently supports only 'docker'")
        image = str(values.get("image", DEFAULT_SANDBOX_IMAGE)).strip()
        network = str(values.get("network", "none")).strip()
        memory = str(values.get("memory", "1g")).strip()
        if not image or not network or not memory:
            raise ValueError("sandbox image, network and memory must not be empty")

        cpus = float(values.get("cpus", 1))
        pids_limit = int(values.get("pidsLimit", 128))
        timeout = float(values.get("commandTimeoutSeconds", 120))
        max_output = int(values.get("maxOutputBytes", 1_048_576))
        if cpus <= 0 or pids_limit <= 0 or timeout <= 0 or max_output <= 0:
            raise ValueError("sandbox resource limits must be positive")

        return cls(
            backend=backend,
            image=image,
            network=network,
            memory=memory,
            cpus=cpus,
            pids_limit=pids_limit,
            command_timeout_seconds=timeout,
            max_output_bytes=max_output,
            unsafe_local=unsafe_local,
            workspace=(workspace or Path.cwd()).resolve(),
        )

    def event_limits(self) -> dict[str, Any]:
        return {
            "backend": "unsafe-local" if self.unsafe_local else self.backend,
            "network": self.network,
            "memory": self.memory,
            "cpus": self.cpus,
            "pidsLimit": self.pids_limit,
            "commandTimeoutSeconds": self.command_timeout_seconds,
            "maxOutputBytes": self.max_output_bytes,
        }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_sandbox_config(
    *, workspace: Path | None = None, unsafe_local: bool = False
) -> SandboxConfig:
    """合并用户级和项目级 sandbox 配置，项目配置优先。"""
    root = (workspace or Path.cwd()).resolve()
    merged: dict[str, Any] = {}
    for path in (Path.home() / ".bear" / "settings.json", root / ".bear" / "settings.json"):
        section = _read_json(path).get("sandbox")
        if isinstance(section, dict):
            merged.update(section)
    return SandboxConfig.from_mapping(merged, workspace=root, unsafe_local=unsafe_local)


@dataclass(frozen=True)
class ExecRequest:
    """Shell 或 stdio 子进程的执行请求。"""

    command: str
    args: tuple[str, ...] = ()
    cwd: str = "/workspace"
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class ExecResult:
    execution_id: str
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    cancelled: bool
    output_truncated: bool
    sandbox_id: str


StdioProcess = asyncio.subprocess.Process
EventSink = Callable[..., Any]


def _command_summary(request: ExecRequest) -> dict[str, Any]:
    if request.args:
        executable = Path(request.command).name
        normalized = "\0".join((request.command, *request.args))
        argument_count = len(request.args)
    else:
        try:
            tokens = shlex.split(request.command)
        except ValueError:
            tokens = request.command.split()
        command_tokens = list(tokens)
        while command_tokens and "=" in command_tokens[0]:
            name, _, _ = command_tokens[0].partition("=")
            if not _ENV_NAME.fullmatch(name):
                break
            command_tokens.pop(0)
        executable = Path(command_tokens[0]).name if command_tokens else "(shell)"
        normalized = request.command
        argument_count = max(0, len(command_tokens) - 1)
    return {
        "executable": executable[:80],
        "argumentCount": argument_count,
        "commandSha256": hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16],
    }


async def _terminate_process(process: asyncio.subprocess.Process, *, process_group: bool) -> None:
    if process.returncode is not None:
        return
    try:
        if process_group and os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        await asyncio.wait_for(process.wait(), timeout=2)
    except (ProcessLookupError, asyncio.TimeoutError):
        if process.returncode is None:
            try:
                if process_group and os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


async def _capture_bounded(
    process: asyncio.subprocess.Process, max_bytes: int
) -> tuple[bytes, bytes, bool]:
    """持续排空两个管道，但只保留总计 max_bytes，避免无界内存增长。"""
    remaining = max_bytes
    truncated = False

    async def read(stream: asyncio.StreamReader | None) -> bytes:
        nonlocal remaining, truncated
        chunks: list[bytes] = []
        if stream is None:
            return b""
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            keep_length = min(len(chunk), remaining)
            if keep_length:
                kept = chunk[:keep_length]
                chunks.append(kept)
                remaining -= len(kept)
            if keep_length < len(chunk):
                truncated = True
        return b"".join(chunks)

    stdout, stderr, _ = await asyncio.gather(
        read(process.stdout), read(process.stderr), process.wait()
    )
    return stdout, stderr, truncated


class SandboxSession:
    """一个主 Agent Session 对应的持久 Docker 容器或显式本地执行器。"""

    def __init__(
        self,
        config: SandboxConfig,
        *,
        session_id: str | None = None,
        event_sink: EventSink | None = None,
    ):
        self.config = config
        self.session_id = session_id or uuid.uuid4().hex[:8]
        suffix = uuid.uuid4().hex[:8]
        self.sandbox_id = "unsafe-local" if config.unsafe_local else f"bearcode-{self.session_id}-{suffix}"
        self._event_sink = event_sink
        self._container_id: str | None = None
        self._started = False
        self._closed = False
        self._start_lock = asyncio.Lock()
        self._restart_lock = asyncio.Lock()

    def _emit(
        self,
        event_type: str,
        *,
        status: str = "completed",
        duration_ms: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(
                event_type,
                status=status,
                duration_ms=duration_ms,
                payload={"sandboxId": self.sandbox_id, **(payload or {})},
            )
        except Exception:
            return

    @property
    def started(self) -> bool:
        return self._started

    @property
    def status(self) -> str:
        """返回面向 UI 的执行面状态，不探测或唤醒 Docker。"""
        if self._closed:
            return "closed"
        if self.config.unsafe_local:
            return "unsafe-local"
        if self._started:
            return "running"
        if self._container_id is not None:
            return "stopped"
        return "not-started"

    def _visible_workspace_secret_files(self) -> list[str]:
        """仅列出根目录中常见的 Secret 文件名，不读取文件内容。"""
        try:
            entries = self.config.workspace.iterdir()
        except OSError:
            return []
        visible: list[str] = []
        for entry in entries:
            name = entry.name
            is_secret = name in _WORKSPACE_SECRET_NAMES or (
                (name == ".env" or name.startswith(".env."))
                and not name.endswith(_WORKSPACE_SECRET_TEMPLATE_SUFFIXES)
            )
            if is_secret:
                visible.append(name)
        return sorted(visible)

    def snapshot(self) -> dict[str, Any]:
        """返回可安全暴露给 Web 的配置与状态摘要。"""
        materialized = self._started or self._container_id is not None
        return {
            "status": self.status,
            "backend": "unsafe-local" if self.config.unsafe_local else self.config.backend,
            "sandboxId": self.sandbox_id if materialized else None,
            "image": None if self.config.unsafe_local else self.config.image,
            "network": "host" if self.config.unsafe_local else self.config.network,
            "workspaceSecretFilesVisible": self._visible_workspace_secret_files(),
        }

    async def _docker(self, *args: str, timeout: float = 30) -> tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise SandboxError(
                "Docker CLI is unavailable. Install/start Docker or explicitly use --unsafe-local."
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            await _terminate_process(process, process_group=False)
            raise SandboxError(f"Docker command timed out: docker {args[0]}") from exc
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )

    async def start(self) -> None:
        """懒创建执行环境；失败时保持 fail closed。"""
        async with self._start_lock:
            if self._started:
                return
            if self._closed:
                raise SandboxError("Sandbox session is already closed")
            started = time.perf_counter()
            if self.config.unsafe_local:
                self._started = True
                self._emit(
                    "sandbox.created",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    payload={
                        **self.config.event_limits(),
                        "warning": "host execution explicitly enabled",
                        "workspaceSecretFilesVisible": self._visible_workspace_secret_files(),
                    },
                )
                return

            code, _, stderr = await self._docker("image", "inspect", self.config.image)
            if code != 0:
                raise SandboxError(
                    f"Docker sandbox image '{self.config.image}' is unavailable. "
                    "Build it with: docker build -f Dockerfile.sandbox -t "
                    f"{self.config.image} . ({stderr or 'image inspect failed'})"
                )

            uid = os.getuid() if hasattr(os, "getuid") and os.getuid() != 0 else 65532
            gid = os.getgid() if hasattr(os, "getgid") and os.getgid() != 0 else 65532
            workspace = str(self.config.workspace)
            args = [
                "create",
                "--name", self.sandbox_id,
                "--label", "bearcode.sandbox=true",
                "--init",
                "--stop-timeout", "1",
                "--network", self.config.network,
                "--memory", self.config.memory,
                "--memory-swap", self.config.memory,
                "--cpus", str(self.config.cpus),
                "--pids-limit", str(self.config.pids_limit),
                "--read-only",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
                "--user", f"{uid}:{gid}",
                "--workdir", "/workspace",
                "--env", "HOME=/tmp",
                "--env", "LANG=C.UTF-8",
                "--env", "LC_ALL=C.UTF-8",
                "--env", "CI=1",
                "--volume", f"{workspace}:/workspace:rw",
                self.config.image,
                "sh", "-c", "trap : TERM INT; sleep infinity & wait",
            ]
            code, container_id, stderr = await self._docker(*args)
            if code != 0:
                self._emit("sandbox.violation", status="failed", payload={"reason": "create failed"})
                raise SandboxError(
                    f"Docker sandbox creation failed for image '{self.config.image}': {stderr or 'unknown error'}"
                )
            self._container_id = container_id
            code, _, stderr = await self._docker("start", self.sandbox_id)
            if code != 0:
                await self._docker("rm", "-f", self.sandbox_id)
                self._container_id = None
                raise SandboxError(f"Docker sandbox start failed: {stderr or 'unknown error'}")
            self._started = True
            self._emit(
                "sandbox.created",
                duration_ms=int((time.perf_counter() - started) * 1000),
                payload={
                    "image": self.config.image,
                    **self.config.event_limits(),
                    "workspaceSecretFilesVisible": self._visible_workspace_secret_files(),
                },
            )

    def _request_env_args(self, env: dict[str, str]) -> list[str]:
        args: list[str] = []
        for key, value in env.items():
            if not _ENV_NAME.fullmatch(key):
                raise SandboxError(f"Invalid environment variable name: {key!r}")
            args.extend(("--env", f"{key}={value}"))
        return args

    async def exec(self, request: ExecRequest) -> ExecResult:
        """执行 Shell 命令并返回稳定结构；超时/取消后重启容器清理进程树。"""
        await self.start()
        execution_id = uuid.uuid4().hex
        summary = _command_summary(request)
        started = time.perf_counter()
        timeout = min(
            request.timeout_seconds or self.config.command_timeout_seconds,
            self.config.command_timeout_seconds,
        )
        self._emit(
            "sandbox.exec.started",
            status="running",
            payload={"executionId": execution_id, "timeoutSeconds": timeout, **summary},
        )

        if self.config.unsafe_local:
            process = await asyncio.create_subprocess_shell(
                request.command,
                cwd=str(self.config.workspace),
                env={**os.environ, **request.env},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            process_group = True
        else:
            args = [
                "exec", "--workdir", request.cwd,
                *self._request_env_args(request.env),
                self.sandbox_id,
                "sh", "-lc", request.command,
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    "docker", *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise SandboxError("Docker CLI disappeared while the sandbox was running") from exc
            process_group = False

        timed_out = False
        try:
            stdout, stderr, truncated = await asyncio.wait_for(
                _capture_bounded(process, self.config.max_output_bytes), timeout=timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            await _terminate_process(process, process_group=process_group)
            await self.restart("command timeout")
            stdout, stderr, truncated = b"", f"Command timed out after {timeout:g}s".encode(), False
        except asyncio.CancelledError:
            await _terminate_process(process, process_group=process_group)
            await asyncio.shield(self.restart("command cancelled"))
            self._emit(
                "sandbox.exec.failed",
                status="cancelled",
                duration_ms=int((time.perf_counter() - started) * 1000),
                payload={"executionId": execution_id, "cancelled": True, **summary},
            )
            raise

        duration_ms = int((time.perf_counter() - started) * 1000)
        exit_code = 124 if timed_out else int(process.returncode or 0)
        result = ExecResult(
            execution_id=execution_id,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=timed_out,
            cancelled=False,
            output_truncated=truncated,
            sandbox_id=self.sandbox_id,
        )
        event_type = "sandbox.exec.completed" if exit_code == 0 else "sandbox.exec.failed"
        self._emit(
            event_type,
            status="completed" if exit_code == 0 else "failed",
            duration_ms=duration_ms,
            payload={
                "executionId": execution_id,
                "exitCode": exit_code,
                "timedOut": timed_out,
                "outputTruncated": truncated,
                **summary,
            },
        )
        return result

    async def spawn_stdio(self, request: ExecRequest) -> StdioProcess:
        """在同一 Sandbox 中启动 MCP 等长驻 stdio 子进程。"""
        await self.start()
        if not request.command:
            raise SandboxError("stdio command must not be empty")
        if self.config.unsafe_local:
            return await asyncio.create_subprocess_exec(
                request.command, *request.args,
                cwd=str(self.config.workspace),
                env={**os.environ, **request.env},
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
        args = [
            "exec", "-i", "--workdir", request.cwd,
            *self._request_env_args(request.env),
            self.sandbox_id,
            request.command, *request.args,
        ]
        return await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def restart(self, reason: str) -> None:
        """重启整个执行面，确保容器中的进程树全部消失。"""
        if not self._started or self._closed:
            return
        async with self._restart_lock:
            started = time.perf_counter()
            if not self.config.unsafe_local:
                await self._docker("kill", self.sandbox_id)
                code, _, stderr = await self._docker("start", self.sandbox_id)
                if code != 0:
                    self._started = False
                    raise SandboxError(f"Docker sandbox restart failed: {stderr or 'unknown error'}")
            self._emit(
                "sandbox.restarted",
                duration_ms=int((time.perf_counter() - started) * 1000),
                payload={"reason": reason},
            )

    async def close(self) -> None:
        """删除会话容器；多次调用安全。"""
        if self._closed:
            return
        self._closed = True
        had_execution_plane = self._started or self._container_id is not None
        started = time.perf_counter()
        if self._container_id is not None and not self.config.unsafe_local:
            try:
                code, _, stderr = await self._docker("rm", "-f", self.sandbox_id)
            except Exception:
                self._closed = False
                raise
            if code != 0:
                self._closed = False
                self._emit(
                    "sandbox.violation",
                    status="failed",
                    payload={"reason": "container cleanup failed"},
                )
                raise SandboxError(
                    f"Docker sandbox cleanup failed: {stderr or 'unknown error'}"
                )
        self._started = False
        self._container_id = None
        if had_execution_plane:
            self._emit(
                "sandbox.destroyed",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
