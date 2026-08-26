"""Bear Code 本地 Web Runtime Manager 与 FastAPI/SSE 接口。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent
from .runtime_events import EventJournal
from .session import load_session, list_sessions

PERMISSION_MODES = {"default", "acceptEdits", "dontAsk", "bypassPermissions", "plan"}
PLAN_CHOICES = {"clear-and-execute", "execute", "manual-execute", "keep-planning"}


class MessageBody(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class SessionBody(BaseModel):
    permissionMode: str | None = None


class PatchSessionBody(BaseModel):
    permissionMode: str


class ApprovalBody(BaseModel):
    decision: str
    feedback: str | None = None


@dataclass
class PendingApproval:
    id: str
    kind: str
    message: str
    future: asyncio.Future
    choices: list[str]
    created_at: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "message": self.message,
            "choices": self.choices,
            "createdAt": self.created_at,
        }


@dataclass
class WebSession:
    id: str
    agent: Agent
    journal: EventJournal
    status: str = "idle"
    updated_time: float = field(default_factory=time.time)
    task: asyncio.Task | None = None
    pending: dict[str, PendingApproval] = field(default_factory=dict)


class WebRuntimeManager:
    """当前 cwd 的单进程 Session 管理器；全局只允许一个活跃 turn。"""

    def __init__(
        self,
        *,
        model: str,
        api_base: str | None,
        api_key: str | None,
        use_openai: bool,
        permission_mode: str = "default",
        thinking: bool = False,
        max_cost_usd: float | None = None,
        max_turns: int | None = None,
    ):
        self.model = model
        self.api_base = api_base
        self.api_key = api_key
        self.use_openai = use_openai
        self.default_permission_mode = permission_mode
        self.thinking = thinking
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.cwd = str(Path.cwd())
        self._sessions: dict[str, WebSession] = {}
        self._active: tuple[str, str] | None = None
        self._lock = asyncio.Lock()

    def _new_agent(self, session_id: str, permission_mode: str) -> Agent:
        journal = EventJournal(session_id)
        agent = Agent(
            permission_mode=permission_mode,
            model=self.model,
            thinking=self.thinking,
            max_cost_usd=self.max_cost_usd,
            max_turns=self.max_turns,
            api_base=self.api_base if self.use_openai else None,
            anthropic_base_url=self.api_base if not self.use_openai else None,
            api_key=self.api_key,
            event_sink=journal.publish,
            session_id=session_id,
        )
        state = WebSession(id=session_id, agent=agent, journal=journal)
        self._sessions[session_id] = state
        self._wire_approvals(state)
        return agent

    def _wire_approvals(self, state: WebSession) -> None:
        async def confirm_tool(message: str) -> bool:
            result = await self._request_approval(state, "tool", message, ["allow", "deny"])
            return result.get("decision") == "allow"

        async def approve_plan(plan_content: str) -> dict[str, Any]:
            result = await self._request_approval(state, "plan", plan_content, sorted(PLAN_CHOICES))
            return {"choice": result.get("decision", "manual-execute"), "feedback": result.get("feedback")}

        state.agent.set_confirm_fn(confirm_tool)
        state.agent.set_plan_approval_fn(approve_plan)

    async def _request_approval(
        self,
        state: WebSession,
        kind: str,
        message: str,
        choices: list[str],
    ) -> dict[str, Any]:
        approval_id = uuid.uuid4().hex[:12]
        future = asyncio.get_running_loop().create_future()
        approval = PendingApproval(
            id=approval_id,
            kind=kind,
            message=message,
            future=future,
            choices=choices,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        state.pending[approval_id] = approval
        state.status = "waiting"
        agent = state.agent
        parent = agent._active_tool_span_id or agent._last_main_model_span_id or agent._turn_span_id
        state.journal.publish(
            "approval.requested",
            status="waiting",
            turn_id=agent._active_turn_id,
            span_id=f"approval-{approval_id}",
            parent_span_id=parent,
            payload={"approvalId": approval_id, "kind": kind, "message": message, "choices": choices},
        )
        try:
            result = await future
        except asyncio.CancelledError:
            result = {"decision": "deny" if kind == "tool" else "manual-execute"}
            raise
        finally:
            state.pending.pop(approval_id, None)
            if state.task and not state.task.done():
                state.status = "running"
        state.journal.publish(
            "approval.resolved",
            status="completed",
            turn_id=agent._active_turn_id,
            span_id=f"approval-{approval_id}",
            parent_span_id=parent,
            payload={"approvalId": approval_id, "kind": kind, **result},
        )
        return result

    def _load(self, session_id: str) -> WebSession:
        if session_id in self._sessions:
            return self._sessions[session_id]
        data = load_session(session_id)
        metadata = (data or {}).get("metadata") or {}
        if not data or metadata.get("cwd") != self.cwd:
            raise HTTPException(status_code=404, detail="Session 不存在于当前项目")
        mode = metadata.get("permissionMode") or self.default_permission_mode
        agent = self._new_agent(session_id, mode)
        agent.restore_session(
            {
                "anthropicMessages": data.get("anthropicMessages"),
                "openaiMessages": data.get("openaiMessages"),
                "foldedSessionMemories": data.get("foldedSessionMemories"),
            }
        )
        state = self._sessions[session_id]
        state.status = metadata.get("lastStatus") or "idle"
        if state.status in {"running", "waiting"}:
            state.status = "interrupted"
            state.journal.publish(
                "turn.failed",
                status="interrupted",
                payload={"reason": "服务重启，历史任务已中断"},
            )
        return state

    def create_session(self, permission_mode: str | None = None) -> WebSession:
        mode = permission_mode or self.default_permission_mode
        if mode not in PERMISSION_MODES:
            raise HTTPException(status_code=422, detail="未知权限模式")
        session_id = uuid.uuid4().hex[:12]
        agent = self._new_agent(session_id, mode)
        state = self._sessions[session_id]
        state.journal.publish(
            "session.started",
            span_id=f"session-{session_id}",
            payload={"model": self.model, "protocol": self.protocol, "cwd": self.cwd, "permissionMode": mode},
        )
        agent._auto_save()
        return state

    @property
    def protocol(self) -> str:
        return "openai" if self.use_openai else "anthropic"

    def config(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "protocol": self.protocol,
            "cwd": self.cwd,
            "permissionMode": self.default_permission_mode,
            "runtimeStatus": "running" if self._active else "idle",
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        snapshots = {
            str(item.get("id")): {
                "permissionMode": self.default_permission_mode,
                "protocol": self.protocol,
                "tokenUsage": {"input": 0, "output": 0},
                "lastStatus": "idle",
                **item,
            }
            for item in list_sessions()
            if item.get("cwd") == self.cwd and item.get("id")
        }
        for session_id, state in self._sessions.items():
            snapshots[session_id] = self._summary(state)
        return sorted(
            snapshots.values(),
            key=lambda item: str(item.get("updatedTime") or item.get("startTime") or ""),
            reverse=True,
        )

    def _summary(self, state: WebSession) -> dict[str, Any]:
        agent = state.agent
        return {
            "id": state.id,
            "model": agent.model,
            "cwd": self.cwd,
            "startTime": agent.session_start_time,
            "updatedTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(state.updated_time)),
            "messageCount": agent._get_message_count(),
            "permissionMode": agent.permission_mode,
            "protocol": "openai" if agent.use_openai else "anthropic",
            "tokenUsage": agent.get_token_usage(),
            "lastStatus": state.status,
        }

    @staticmethod
    def _messages(agent: Agent) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        source = agent._openai_messages if agent.use_openai else agent._anthropic_messages
        for item in source:
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            content = item.get("content")
            if isinstance(content, str):
                text = agent._strip_runtime_injections(content)
            elif isinstance(content, list):
                text = "".join(
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                text = ""
            if text.strip():
                messages.append({"role": item["role"], "content": text.strip()})
        return messages

    def detail(self, session_id: str) -> dict[str, Any]:
        state = self._load(session_id)
        return {
            **self._summary(state),
            "messages": self._messages(state.agent),
            "pendingApprovals": [item.public() for item in state.pending.values()],
            "hasTrace": bool(state.journal.read()),
            "lastEventSeq": state.journal.last_seq,
        }

    async def send_message(self, session_id: str, content: str) -> str:
        state = self._load(session_id)
        async with self._lock:
            if self._active is not None:
                raise HTTPException(status_code=409, detail="已有任务正在运行")
            turn_id = uuid.uuid4().hex[:12]
            self._active = (session_id, turn_id)
            state.status = "running"
            state.updated_time = time.time()
            state.task = asyncio.create_task(self._run_turn(state, content))
        return turn_id

    async def _run_turn(self, state: WebSession, content: str) -> None:
        try:
            turn_id = self._active[1] if self._active and self._active[0] == state.id else None
            await state.agent.chat(content, turn_id=turn_id)
            state.status = "aborted" if state.agent._aborted else "completed"
        except asyncio.CancelledError:
            state.status = "aborted"
        except Exception:
            state.status = "failed"
        finally:
            state.updated_time = time.time()
            state.agent._auto_save()
            state.task = None
            async with self._lock:
                if self._active and self._active[0] == state.id:
                    self._active = None

    async def abort(self, session_id: str) -> None:
        state = self._load(session_id)
        if not state.task or state.task.done():
            raise HTTPException(status_code=409, detail="当前 Session 没有运行中的任务")
        state.agent.abort()
        for approval in tuple(state.pending.values()):
            if not approval.future.done():
                approval.future.set_result({"decision": "deny" if approval.kind == "tool" else "manual-execute"})

    async def patch_permission(self, session_id: str, mode: str) -> dict[str, Any]:
        if mode not in PERMISSION_MODES:
            raise HTTPException(status_code=422, detail="未知权限模式")
        state = self._load(session_id)
        if state.task and not state.task.done():
            raise HTTPException(status_code=409, detail="任务运行期间不能切换权限模式")
        state.agent.permission_mode = mode
        state.updated_time = time.time()
        state.agent._refresh_runtime_system_prompt()
        state.agent._auto_save()
        return self.detail(session_id)

    async def resolve_approval(self, session_id: str, approval_id: str, body: ApprovalBody) -> None:
        state = self._load(session_id)
        approval = state.pending.get(approval_id)
        if approval is None or approval.future.done():
            raise HTTPException(status_code=409, detail="审批已失效或服务已重启")
        if body.decision not in approval.choices:
            raise HTTPException(status_code=422, detail="审批选项不适用于当前请求")
        if approval.kind == "tool" and body.decision not in {"allow", "deny"}:
            raise HTTPException(status_code=422, detail="工具审批只接受 allow 或 deny")
        approval.future.set_result({"decision": body.decision, "feedback": body.feedback})

    async def shutdown(self) -> None:
        for state in self._sessions.values():
            if state.task and not state.task.done():
                state.agent.abort()
        tasks = [state.task for state in self._sessions.values() if state.task and not state.task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _sse(event: dict[str, Any]) -> str:
    return f"id: {event['seq']}\nevent: runtime\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_web_app(manager: WebRuntimeManager) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await manager.shutdown()

    app = FastAPI(title="Bear Code Developer Console", version="0.1.0", lifespan=lifespan)
    app.state.runtime_manager = manager
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return manager.config()

    @app.get("/api/sessions")
    async def sessions() -> list[dict[str, Any]]:
        return manager.list_sessions()

    @app.post("/api/sessions", status_code=201)
    async def create_session(body: SessionBody | None = None) -> dict[str, Any]:
        state = manager.create_session(body.permissionMode if body else None)
        return manager.detail(state.id)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return manager.detail(session_id)

    @app.post("/api/sessions/{session_id}/resume")
    async def resume_session(session_id: str) -> dict[str, Any]:
        return manager.detail(session_id)

    @app.patch("/api/sessions/{session_id}")
    async def patch_session(session_id: str, body: PatchSessionBody) -> dict[str, Any]:
        return await manager.patch_permission(session_id, body.permissionMode)

    @app.post("/api/sessions/{session_id}/messages", status_code=202)
    async def post_message(session_id: str, body: MessageBody) -> dict[str, str]:
        turn_id = await manager.send_message(session_id, body.content.strip())
        return {"turnId": turn_id}

    @app.post("/api/sessions/{session_id}/abort", status_code=202)
    async def abort_session(session_id: str) -> dict[str, str]:
        await manager.abort(session_id)
        return {"status": "aborting"}

    @app.post("/api/sessions/{session_id}/approvals/{approval_id}", status_code=202)
    async def approval(session_id: str, approval_id: str, body: ApprovalBody) -> dict[str, str]:
        await manager.resolve_approval(session_id, approval_id, body)
        return {"status": "accepted"}

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(
        request: Request,
        session_id: str,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        state = manager._load(session_id)
        cursor = after
        if last_event_id:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError:
                pass

        async def generate():
            nonlocal cursor
            queue = state.journal.subscribe()
            try:
                for event in state.journal.read(cursor):
                    cursor = max(cursor, int(event["seq"]))
                    yield _sse(event)
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if int(event.get("seq", 0)) <= cursor:
                        continue
                    cursor = int(event["seq"])
                    yield _sse(event)
            finally:
                state.journal.unsubscribe(queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    web_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
    if web_dist.exists():
        assets = web_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/", include_in_schema=False)
        async def web_index() -> FileResponse:
            return FileResponse(web_dist / "index.html", headers={"Cache-Control": "no-cache"})

        @app.get("/{path:path}", include_in_schema=False)
        async def web_fallback(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(status_code=404, detail="API endpoint not found")
            return FileResponse(web_dist / "index.html", headers={"Cache-Control": "no-cache"})
    else:
        @app.get("/", include_in_schema=False)
        async def missing_frontend() -> HTMLResponse:
            return HTMLResponse(
                "<h1>Bear Code Developer Console</h1>"
                "<p>前端尚未构建。请运行 <code>npm --prefix web install && npm --prefix web run build</code>。</p>",
                status_code=503,
            )

    return app
