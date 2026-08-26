import type { RuntimeEvent, TraceNode } from "./types";

const TERMINAL_PRIORITY = ["failed", "denied", "aborted", "interrupted", "completed", "running", "proposed"];

export function mergeEvent(events: RuntimeEvent[], incoming: RuntimeEvent): RuntimeEvent[] {
  if (incoming.type === "assistant.delta" || incoming.type === "thinking.delta") {
    const index = events.findIndex(
      (event) => event.type === incoming.type && event.spanId === incoming.spanId && event.turnId === incoming.turnId,
    );
    if (index >= 0) {
      const next = [...events];
      const previous = next[index];
      next[index] = {
        ...incoming,
        id: previous.id,
        payload: {
          ...previous.payload,
          ...incoming.payload,
          text: `${String(previous.payload.text ?? "")}${String(incoming.payload.text ?? "")}`,
        },
      };
      return next;
    }
  }
  if (events.some((event) => event.id === incoming.id)) return events;
  if (!events.length || events[events.length - 1].seq <= incoming.seq) return [...events, incoming];
  return [...events, incoming].sort((a, b) => a.seq - b.seq);
}

function preferredEvent(current: RuntimeEvent, candidate: RuntimeEvent): RuntimeEvent {
  const currentRank = TERMINAL_PRIORITY.indexOf(current.status);
  const candidateRank = TERMINAL_PRIORITY.indexOf(candidate.status);
  if (candidateRank >= 0 && (currentRank < 0 || candidateRank < currentRank)) return candidate;
  return candidate.seq >= current.seq && candidate.type !== "tool.proposed" ? candidate : current;
}

export function buildTrace(events: RuntimeEvent[]): TraceNode[] {
  const traceable = events.filter(
    (event) =>
      !["assistant.delta", "thinking.delta", "budget.updated", "session.started"].includes(event.type) &&
      Boolean(event.spanId),
  );
  const nodes = new Map<string, TraceNode>();
  for (const event of traceable) {
    const existing = nodes.get(event.spanId);
    if (existing) {
      existing.event = preferredEvent(existing.event, event);
      existing.firstSeq = Math.min(existing.firstSeq, event.seq);
    } else {
      nodes.set(event.spanId, { id: event.spanId, event, children: [], firstSeq: event.seq });
    }
  }
  const roots: TraceNode[] = [];
  for (const node of nodes.values()) {
    const parent = node.event.parentSpanId ? nodes.get(node.event.parentSpanId) : undefined;
    if (parent && parent.id !== node.id) parent.children.push(node);
    else roots.push(node);
  }
  const sortNodes = (items: TraceNode[]) => {
    items.sort((a, b) => a.firstSeq - b.firstSeq);
    items.forEach((item) => sortNodes(item.children));
  };
  sortNodes(roots);
  return roots;
}

export function eventFamily(type: string): string {
  return type.split(".")[0];
}

export function eventLabel(event: RuntimeEvent): string {
  const payload = event.payload;
  if (event.type.startsWith("tool.")) return String(payload.name ?? "工具调用");
  if (event.type.startsWith("model.")) return String(payload.purpose ?? "模型推理");
  if (event.type.startsWith("subagent.")) return String(payload.description ?? payload.type ?? "子 Agent");
  if (event.type.startsWith("approval.")) return String(payload.kind === "plan" ? "Plan 审批" : "权限审批");
  if (event.type === "memory.recalled") return "Memory 召回";
  if (event.type === "skill.retrieved") return String(payload.name ?? "Skill 检索");
  if (event.type.startsWith("context.")) return "上下文压缩";
  if (event.type.startsWith("turn.")) return "用户任务";
  if (event.type === "assistant.delta") return "回复输出";
  if (event.type === "thinking.delta") return "Thinking";
  return event.type;
}

export function eventIcon(type: string): string {
  const family = eventFamily(type);
  return ({ turn: "↳", model: "✦", tool: "⌘", approval: "!", subagent: "◇", memory: "◌", skill: "◆", context: "⊙", assistant: "✎", thinking: "∿" } as Record<string, string>)[family] ?? "·";
}

export function formatDuration(duration: number | null): string {
  if (duration == null) return "";
  if (duration < 1000) return `${duration} ms`;
  return `${(duration / 1000).toFixed(duration < 10_000 ? 1 : 0)} s`;
}
