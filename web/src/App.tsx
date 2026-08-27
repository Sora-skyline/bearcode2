import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { buildTrace, eventFamily, eventIcon, eventLabel, formatDuration, mergeEvent } from "./events";
import type { AppConfig, ChatMessage, PendingApproval, RuntimeEvent, SessionDetail, SessionSummary, TraceNode } from "./types";

const PERMISSIONS = [
  { value: "default", label: "默认", hint: "危险操作需确认" },
  { value: "acceptEdits", label: "接受编辑", hint: "自动允许文件编辑" },
  { value: "dontAsk", label: "不询问", hint: "自动拒绝危险操作" },
  { value: "plan", label: "Plan", hint: "只读规划模式" },
  { value: "bypassPermissions", label: "YOLO", hint: "跳过确认" },
];

const FAMILY_FILTERS = ["all", "model", "tool", "approval", "subagent", "memory", "skill", "context"];
const EVENT_RENDER_LIMIT = 500;

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail ?? "请求失败");
  }
  return response.json() as Promise<T>;
}

function shortPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts[parts.length - 1] || path;
}

function timeLabel(value?: string): string {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "刚刚";
  const now = new Date();
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  }
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

function statusText(status: string): string {
  return ({ idle: "空闲", running: "运行中", waiting: "等待审批", completed: "已完成", failed: "失败", aborted: "已中止", interrupted: "已中断", denied: "已拒绝", proposed: "待执行", streaming: "输出中" } as Record<string, string>)[status] ?? status;
}

function sessionTitle(session: SessionSummary): string {
  if (session.lastStatus === "running") return "正在处理任务";
  return `Session ${session.id.slice(0, 5)}`;
}

const SANDBOX_STATUS_LABELS: Record<string, string> = {
  "not-started": "未启动",
  running: "运行中",
  stopped: "已停止",
  closed: "已关闭",
  "unsafe-local": "本地不隔离",
};

function TraceItem({ node, depth, onSelect }: { node: TraceNode; depth: number; onSelect: (event: RuntimeEvent) => void }) {
  const [open, setOpen] = useState(depth < 2);
  const hasChildren = node.children.length > 0;
  return (
    <div className="trace-node">
      <button className="trace-row" style={{ paddingLeft: `${12 + depth * 18}px` }} onClick={() => onSelect(node.event)}>
        <span
          className={`trace-caret ${hasChildren ? "" : "empty"}`}
          onClick={(event) => { event.stopPropagation(); if (hasChildren) setOpen((value) => !value); }}
        >{open ? "⌄" : "›"}</span>
        <span className={`event-glyph family-${eventFamily(node.event.type)}`}>{eventIcon(node.event.type)}</span>
        <span className="trace-copy">
          <span>{eventLabel(node.event)}</span>
          <small>{node.event.type}</small>
        </span>
        <span className={`mini-status status-${node.event.status}`}>{formatDuration(node.event.durationMs) || "·"}</span>
      </button>
      {open && node.children.map((child) => <TraceItem key={child.id} node={child} depth={depth + 1} onSelect={onSelect} />)}
    </div>
  );
}

function EventDetails({ event, onClose }: { event: RuntimeEvent; onClose: () => void }) {
  const isTool = event.type.startsWith("tool.");
  const isThinking = event.type === "thinking.delta";
  return (
    <div className="drawer-backdrop" onMouseDown={onClose}>
      <aside className="event-drawer" onMouseDown={(mouseEvent) => mouseEvent.stopPropagation()}>
        <header className="drawer-head">
          <div>
            <span className="eyebrow">EVENT DETAIL</span>
            <h2>{eventLabel(event)}</h2>
            <code>{event.type}</code>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="关闭详情">×</button>
        </header>
        <div className="drawer-body">
          <div className="detail-grid">
            <div><span>状态</span><strong className={`status-text status-${event.status}`}>{statusText(event.status)}</strong></div>
            <div><span>耗时</span><strong>{formatDuration(event.durationMs) || "—"}</strong></div>
            <div><span>Agent</span><strong>{event.agentId}</strong></div>
            <div><span>序号</span><strong>#{event.seq}</strong></div>
          </div>
          <section className="detail-section">
            <h3>时间</h3>
            <p>{new Date(event.timestamp).toLocaleString("zh-CN", { hour12: false })}</p>
          </section>
          {isTool && <>
            <section className="detail-section"><h3>输入</h3><pre>{JSON.stringify(event.payload.input ?? {}, null, 2)}</pre></section>
            {(event.payload.result != null || event.payload.error != null) && <section className="detail-section"><h3>结果</h3><pre>{String(event.payload.result ?? event.payload.error)}</pre></section>}
          </>}
          {event.type.startsWith("model.") && <section className="detail-section">
            <h3>模型调用</h3>
            <dl className="model-facts">
              <div><dt>模型</dt><dd>{String(event.payload.model ?? "—")}</dd></div>
              <div><dt>协议</dt><dd>{String(event.payload.protocol ?? "—")}</dd></div>
              <div><dt>用途</dt><dd>{String(event.payload.purpose ?? "main")}</dd></div>
              <div><dt>停止原因</dt><dd>{String(event.payload.stopReason ?? "—")}</dd></div>
              <div><dt>Token</dt><dd>{JSON.stringify(event.payload.tokenUsage ?? {})}</dd></div>
            </dl>
          </section>}
          {isThinking && <details className="thinking-detail"><summary>Thinking（默认折叠）</summary><p className="notice">仅展示模型接口实际返回的内容</p><pre>{String(event.payload.text ?? "")}</pre></details>}
          <section className="detail-section raw-json"><h3>原始 RuntimeEvent</h3><pre>{JSON.stringify(event, null, 2)}</pre></section>
        </div>
      </aside>
    </div>
  );
}

function ApprovalCard({ approval, onResolve, busy }: { approval: PendingApproval; onResolve: (decision: string, feedback?: string) => void; busy: boolean }) {
  const [feedback, setFeedback] = useState("");
  const isPlan = approval.kind === "plan";
  return (
    <section className="approval-card">
      <div className="approval-icon">!</div>
      <div className="approval-content">
        <div className="approval-title"><strong>{isPlan ? "Plan 等待审批" : "工具需要权限"}</strong><span>任务仍保持连接</span></div>
        <pre className={isPlan ? "plan-preview" : "command-preview"}>{approval.message}</pre>
        {isPlan && <textarea className="feedback-input" value={feedback} onChange={(event) => setFeedback(event.target.value)} placeholder="选择继续规划时，可填写修改意见…" />}
        <div className="approval-actions">
          {isPlan ? <>
            <button disabled={busy} className="primary-small" onClick={() => onResolve("execute")}>批准并执行</button>
            <button disabled={busy} onClick={() => onResolve("clear-and-execute")}>清空上下文后执行</button>
            <button disabled={busy} onClick={() => onResolve("manual-execute")}>仅退出 Plan</button>
            <button disabled={busy} onClick={() => onResolve("keep-planning", feedback)}>继续规划</button>
          </> : <>
            <button disabled={busy} className="primary-small" onClick={() => onResolve("allow")}>允许</button>
            <button disabled={busy} className="danger-small" onClick={() => onResolve("deny")}>拒绝</button>
          </>}
        </div>
      </div>
    </section>
  );
}

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [events, setEvents] = useState<RuntimeEvent[]>([]);
  const [pending, setPending] = useState<PendingApproval[]>([]);
  const [draft, setDraft] = useState("");
  const [tab, setTab] = useState<"events" | "trace">("events");
  const [familyFilter, setFamilyFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [selectedEvent, setSelectedEvent] = useState<RuntimeEvent | null>(null);
  const [error, setError] = useState("");
  const [actionBusy, setActionBusy] = useState(false);
  const [streamConnected, setStreamConnected] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const maxSeqRef = useRef(0);
  const bootedRef = useRef(false);

  const refreshSessions = async () => {
    const result = await api<SessionSummary[]>("/api/sessions");
    setSessions(result);
    return result;
  };

  const createSession = async () => {
    setActionBusy(true);
    try {
      const created = await api<SessionDetail>("/api/sessions", { method: "POST", body: JSON.stringify({}) });
      await refreshSessions();
      setSessionId(created.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "新建 Session 失败");
    } finally {
      setActionBusy(false);
    }
  };

  useEffect(() => {
    if (bootedRef.current) return;
    bootedRef.current = true;
    Promise.all([api<AppConfig>("/api/config"), refreshSessions()])
      .then(([nextConfig, nextSessions]) => {
        setConfig(nextConfig);
        if (nextSessions.length) setSessionId(nextSessions[0].id);
        else void createSession();
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "控制台初始化失败"));
  }, []);

  useEffect(() => {
    if (!sessionId) return;
    let closed = false;
    let source: EventSource | null = null;
    let approvalSnapshotSeq = 0;
    maxSeqRef.current = 0;
    setDetail(null);
    setEvents([]);
    setMessages([]);
    setPending([]);
    setStreamConnected(false);
    const onRuntime = (raw: MessageEvent<string>) => {
      let incoming: RuntimeEvent;
      try {
        incoming = JSON.parse(raw.data) as RuntimeEvent;
      } catch {
        setError("收到无法解析的 RuntimeEvent");
        return;
      }
      if (incoming.seq <= maxSeqRef.current) return;
      maxSeqRef.current = Math.max(maxSeqRef.current, incoming.seq);
      setEvents((current) => mergeEvent(current, incoming));
      if (incoming.agentId === "main" && incoming.type === "sandbox.created") {
        setDetail((current) => current?.sandbox ? {
          ...current,
          sandbox: {
            ...current.sandbox,
            status: incoming.payload.backend === "unsafe-local" ? "unsafe-local" : "running",
            sandboxId: String(incoming.payload.sandboxId ?? current.sandbox.sandboxId),
          },
        } : current);
      }
      if (incoming.agentId === "main" && incoming.type === "sandbox.restarted") {
        setDetail((current) => current?.sandbox && current.sandbox.backend === "docker"
          ? { ...current, sandbox: { ...current.sandbox, status: "running" } }
          : current);
      }
      if (incoming.agentId === "main" && incoming.type === "sandbox.destroyed") {
        setDetail((current) => current?.sandbox
          ? { ...current, sandbox: { ...current.sandbox, status: "closed", sandboxId: null } }
          : current);
      }
      if (incoming.agentId === "main" && incoming.type === "turn.started") {
        const content = String(incoming.payload.userMessage ?? "");
        setMessages((current) => current.some((item) => item.turnId === incoming.turnId && item.role === "user")
          ? current
          : [...current, { id: incoming.id, role: "user", content, turnId: incoming.turnId, status: "completed" }]);
        setDetail((current) => current ? { ...current, lastStatus: "running" } : current);
      }
      if (incoming.agentId === "main" && incoming.type === "assistant.delta") {
        const text = String(incoming.payload.text ?? "");
        setMessages((current) => {
          const index = current.findIndex((item) => item.turnId === incoming.turnId && item.role === "assistant");
          if (index < 0) return [...current, { id: `assistant-${incoming.turnId}`, role: "assistant", content: text, turnId: incoming.turnId, status: "streaming" }];
          const next = [...current];
          next[index] = { ...next[index], content: next[index].content + text, status: "streaming" };
          return next;
        });
      }
      if (incoming.agentId === "main" && incoming.type.startsWith("turn.") && incoming.type !== "turn.started") {
        setMessages((current) => {
          const index = current.findIndex((item) => item.turnId === incoming.turnId && item.role === "assistant");
          const finalText = String(incoming.payload.assistantText ?? "");
          if (index < 0 && finalText) return [...current, { id: `assistant-${incoming.turnId}`, role: "assistant", content: finalText, turnId: incoming.turnId, status: incoming.status }];
          if (index < 0) return current;
          const next = [...current];
          next[index] = { ...next[index], content: finalText || next[index].content, status: incoming.status };
          return next;
        });
        setDetail((current) => current ? { ...current, lastStatus: incoming.status } : current);
        void refreshSessions();
      }
      if (incoming.seq > approvalSnapshotSeq && incoming.type === "approval.requested") {
        const approval: PendingApproval = {
          id: String(incoming.payload.approvalId),
          kind: incoming.payload.kind === "plan" ? "plan" : "tool",
          message: String(incoming.payload.message ?? ""),
          choices: Array.isArray(incoming.payload.choices) ? incoming.payload.choices.map(String) : [],
          createdAt: incoming.timestamp,
        };
        setPending((current) => current.some((item) => item.id === approval.id) ? current : [...current, approval]);
      }
      if (incoming.seq > approvalSnapshotSeq && incoming.type === "approval.resolved") {
        setPending((current) => current.filter((item) => item.id !== String(incoming.payload.approvalId)));
      }
    };

    const connect = async () => {
      try {
        const nextDetail = await api<SessionDetail>(`/api/sessions/${sessionId}/resume`, { method: "POST" });
        if (closed) return;
        setDetail(nextDetail);
        setMessages(nextDetail.hasTrace ? [] : nextDetail.messages);
        setPending(nextDetail.pendingApprovals);
        approvalSnapshotSeq = nextDetail.lastEventSeq ?? 0;
        if (nextDetail.lastStatus === "interrupted") {
          setError("上次任务因服务重启中断，请重新发送任务");
        }
      } catch (reason) {
        if (!closed) setError(reason instanceof Error ? reason.message : "恢复 Session 失败");
        return;
      }
      if (closed) return;
      source = new EventSource(`/api/sessions/${sessionId}/events?after=0`);
      source.addEventListener("runtime", onRuntime as EventListener);
      source.onopen = () => { if (!closed) setStreamConnected(true); };
      source.onerror = () => { if (!closed) setStreamConnected(false); };
    };
    void connect();

    return () => {
      closed = true;
      source?.removeEventListener("runtime", onRuntime as EventListener);
      source?.close();
    };
  }, [sessionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pending]);

  const running = detail?.lastStatus === "running" || detail?.lastStatus === "waiting";
  const traces = useMemo(() => tab === "trace" ? buildTrace(events) : [], [events, tab]);
  const filteredEvents = useMemo(
    () => events.filter((event) =>
      event.type !== "session.started" &&
      (familyFilter === "all" || eventFamily(event.type) === familyFilter) &&
      (statusFilter === "all" || event.status === statusFilter),
    ),
    [events, familyFilter, statusFilter],
  );
  const hiddenEventCount = Math.max(0, filteredEvents.length - EVENT_RENDER_LIMIT);
  const visibleEvents = useMemo(
    () => filteredEvents.slice(-EVENT_RENDER_LIMIT),
    [filteredEvents],
  );
  const eventGroups = useMemo(() => {
    const groups: { id: string; label: string; events: RuntimeEvent[] }[] = [];
    const groupsById = new Map<string, (typeof groups)[number]>();
    const turnLabels = new Map(
      events
        .filter((event) => event.type === "turn.started" && event.turnId)
        .map((event) => [event.turnId as string, String(event.payload.userMessage ?? "用户任务")]),
    );
    for (const event of visibleEvents) {
      const id = event.turnId ?? "session";
      let group = groupsById.get(id);
      if (!group) {
        group = { id, label: turnLabels.get(id) ?? "Session 事件", events: [] };
        groupsById.set(id, group);
        groups.push(group);
      }
      group.events.push(event);
    }
    return groups;
  }, [visibleEvents, events]);

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    const content = draft.trim();
    if (!sessionId || !content || running) return;
    setDraft("");
    setError("");
    try {
      setDetail((current) => current ? { ...current, lastStatus: "running" } : current);
      await api(`/api/sessions/${sessionId}/messages`, { method: "POST", body: JSON.stringify({ content }) });
    } catch (reason) {
      setDetail((current) => current ? { ...current, lastStatus: "idle" } : current);
      setDraft(content);
      setError(reason instanceof Error ? reason.message : "发送失败");
    }
  };

  const onComposerKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  };

  const changePermission = async (permissionMode: string) => {
    if (!sessionId || running) return;
    try {
      const updated = await api<SessionDetail>(`/api/sessions/${sessionId}`, { method: "PATCH", body: JSON.stringify({ permissionMode }) });
      setDetail(updated);
      void refreshSessions();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "权限模式切换失败");
    }
  };

  const abort = async () => {
    if (!sessionId) return;
    try {
      await api(`/api/sessions/${sessionId}/abort`, { method: "POST" });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "中止失败");
    }
  };

  const resolveApproval = async (approval: PendingApproval, decision: string, feedback?: string) => {
    if (!sessionId) return;
    setActionBusy(true);
    try {
      await api(`/api/sessions/${sessionId}/approvals/${approval.id}`, {
        method: "POST",
        body: JSON.stringify({ decision, feedback: feedback || null }),
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "审批提交失败");
    } finally {
      setActionBusy(false);
    }
  };

  const currentPermission = PERMISSIONS.find((item) => item.value === detail?.permissionMode) ?? PERMISSIONS[0];

  return (
    <main className="console-shell">
      <aside className="session-rail">
        <header className="brand-block">
          <div className="bear-mark"><span>• ᴥ •</span></div>
          <div><h1>Bear Code</h1><p>Developer Console</p></div>
        </header>
        <button className="new-session" onClick={() => void createSession()} disabled={actionBusy}><span>＋</span> 新建 Session <kbd>⌘ N</kbd></button>
        <div className="rail-section-title"><span>当前项目</span><small>{sessions.length}</small></div>
        <div className="project-chip"><span className="folder-icon">⌁</span><div><strong>{config ? shortPath(config.cwd) : "加载中…"}</strong><small>{config?.cwd ?? ""}</small></div></div>
        <nav className="session-list" aria-label="Session 列表">
          {sessions.map((session) => <button key={session.id} className={`session-item ${session.id === sessionId ? "active" : ""}`} onClick={() => setSessionId(session.id)}>
            <span className={`session-state status-${session.lastStatus}`}></span>
            <span className="session-copy"><strong>{sessionTitle(session)}</strong><small>{session.model} · {session.messageCount} 条消息</small></span>
            <time>{timeLabel(session.updatedTime ?? session.startTime)}</time>
          </button>)}
        </nav>
        <footer className="rail-footer">
          <div className="runtime-card"><span className={`connection-dot ${streamConnected ? "online" : ""}`}></span><div><strong>{config?.model ?? "—"}</strong><small>{config?.protocol ?? "—"} compatible</small></div><span className="local-tag">LOCAL</span></div>
          <button className="settings-link">⚙ <span>本地配置由 .env 管理</span></button>
        </footer>
      </aside>

      <section className="chat-workspace">
        <header className="workspace-head">
          <div className="session-heading"><div className="live-orb"><span></span></div><div><h2>{detail ? sessionTitle(detail) : "准备控制台"}</h2><p>{config ? shortPath(config.cwd) : "Bear Code"} <span>·</span> {detail?.id ?? "—"}</p></div></div>
          <div className="head-actions">
            {detail?.sandbox && <span
              className={`sandbox-status sandbox-status-${detail.sandbox.status}`}
              title={detail.sandbox.workspaceSecretFilesVisible.length
                ? `工作区敏感文件对执行面可见：${detail.sandbox.workspaceSecretFilesVisible.join(", ")}`
                : `Backend: ${detail.sandbox.backend} · Network: ${detail.sandbox.network}`}
            ><i></i>Sandbox {SANDBOX_STATUS_LABELS[detail.sandbox.status] ?? detail.sandbox.status}</span>}
            <label className="permission-select"><span>权限</span><select value={detail?.permissionMode ?? "default"} onChange={(event) => void changePermission(event.target.value)} disabled={!detail || running}>{PERMISSIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
            {running && <button className="abort-button" onClick={() => void abort()}><span>■</span> 中止</button>}
          </div>
        </header>

        <div className="chat-scroll">
          {!messages.length && <section className="empty-state">
            <div className="welcome-bear"><span>• ᴥ •</span></div>
            <p className="eyebrow">LOCAL HARNESS · READY</p>
            <h2>今天想和代码一起<br /><em>解决什么？</em></h2>
            <p>消息、模型、工具与审批会在右侧形成一条可追踪的执行链。</p>
            <div className="starter-grid">
              {["梳理当前项目架构", "定位最近一次测试失败", "制定一个只读改造计划"].map((text, index) => <button key={text} onClick={() => setDraft(text)}><span>{["⌘", "◎", "◇"][index]}</span>{text}<b>↗</b></button>)}
            </div>
          </section>}
          <div className="message-column">
            {messages.map((message, index) => <article key={message.id ?? `${message.role}-${index}`} className={`message message-${message.role}`}>
              <div className="message-avatar">{message.role === "user" ? "你" : <span>•ᴥ•</span>}</div>
              <div className="message-body">
                <div className="message-meta"><strong>{message.role === "user" ? "You" : "Bear Code"}</strong><span>{message.status === "streaming" ? "正在回复…" : ""}</span></div>
                <div className="message-text">{message.content || (message.status === "streaming" ? <span className="typing"><i></i><i></i><i></i></span> : "")}</div>
              </div>
            </article>)}
            {pending.map((approval) => <ApprovalCard key={approval.id} approval={approval} busy={actionBusy} onResolve={(decision, feedback) => void resolveApproval(approval, decision, feedback)} />)}
            <div ref={bottomRef} />
          </div>
        </div>

        <footer className="composer-area">
          {error && <div className="error-toast"><span>!</span>{error}<button onClick={() => setError("")}>×</button></div>}
          <form className={`composer ${running ? "disabled" : ""}`} onSubmit={(event) => void submit(event)}>
            <textarea value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={onComposerKey} disabled={running || !sessionId} placeholder={running ? "Bear Code 正在执行任务…" : "给 Bear Code 一个任务…"} rows={1} />
            <div className="composer-tools"><div><button type="button" title="附加上下文">＋</button><span>{currentPermission.hint}</span></div><button type="submit" className="send-button" disabled={!draft.trim() || running}>↑</button></div>
          </form>
          <p className="composer-note">Enter 发送 · Shift + Enter 换行 · 仅在本机运行</p>
        </footer>
      </section>

      <aside className="inspector-panel">
        <header className="inspector-head">
          <div className="tabs"><button className={tab === "events" ? "active" : ""} onClick={() => setTab("events")}>Events <span>{events.length}</span></button><button className={tab === "trace" ? "active" : ""} onClick={() => setTab("trace")}>Trace</button></div>
          <button className="icon-button" title="清空视图" onClick={() => setEvents([])}>↺</button>
        </header>
        {tab === "events" ? <div className="inspector-content">
          <div className="filter-bar">
            <select value={familyFilter} onChange={(event) => setFamilyFilter(event.target.value)}>{FAMILY_FILTERS.map((filter) => <option key={filter} value={filter}>{filter === "all" ? "所有类型" : filter}</option>)}</select>
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">所有状态</option><option value="completed">已完成</option><option value="running">运行中</option><option value="failed">失败</option><option value="denied">已拒绝</option></select>
          </div>
          {detail && !detail.hasTrace && <div className="legacy-banner">此前没有 Trace 数据；新一轮事件会从这里开始记录。</div>}
          <div className="events-scroll">
            {hiddenEventCount > 0 && <div className="event-limit-banner">为保持页面流畅，仅显示最近 {EVENT_RENDER_LIMIT} 条事件；已隐藏 {hiddenEventCount} 条。</div>}
            {!eventGroups.length && <div className="panel-empty"><span>⌁</span><strong>等待 Runtime 事件</strong><p>发送一条消息后，执行轨迹会出现在这里。</p></div>}
            {eventGroups.map((group, groupIndex) => <section className="event-group" key={`${group.id}-${groupIndex}`}>
              <header><span>TURN {groupIndex + 1}</span><strong>{group.label}</strong><small>{group.events.length}</small></header>
              <div className="event-list">{group.events.map((event) => <button key={event.id} className="event-row" onClick={() => setSelectedEvent(event)}>
                <span className={`event-glyph family-${eventFamily(event.type)}`}>{eventIcon(event.type)}</span>
                <span className="event-copy"><strong>{eventLabel(event)}</strong><small>{event.type} · {event.agentId}</small></span>
                <span className="event-time"><i className={`status-dot status-${event.status}`}></i>{formatDuration(event.durationMs) || timeLabel(event.timestamp)}</span>
              </button>)}</div>
            </section>)}
          </div>
        </div> : <div className="trace-scroll">
          <div className="trace-legend"><span><i className="status-dot status-completed"></i>完成</span><span><i className="status-dot status-running"></i>运行</span><span><i className="status-dot status-failed"></i>失败</span></div>
          {!traces.length && <div className="panel-empty"><span>◇</span><strong>Trace 尚为空</strong><p>模型与工具的父子 Span 会在这里组成树。</p></div>}
          {traces.map((node) => <TraceItem key={node.id} node={node} depth={0} onSelect={setSelectedEvent} />)}
        </div>}
        <footer className="inspector-footer"><span className={`connection-dot ${streamConnected ? "online" : ""}`}></span>{streamConnected ? "SSE 已连接" : "正在重连"}<small>seq {maxSeqRef.current || "—"}</small></footer>
      </aside>
      {selectedEvent && <EventDetails event={selectedEvent} onClose={() => setSelectedEvent(null)} />}
    </main>
  );
}
