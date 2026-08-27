export interface RuntimeEvent {
  schemaVersion: string;
  id: string;
  seq: number;
  timestamp: string;
  sessionId: string;
  turnId: string | null;
  spanId: string;
  parentSpanId: string | null;
  agentId: string;
  type: string;
  status: string;
  durationMs: number | null;
  payload: Record<string, unknown>;
}

export interface SessionSummary {
  id: string;
  model: string;
  cwd: string;
  startTime: string;
  updatedTime?: string;
  messageCount: number;
  permissionMode: string;
  protocol: string;
  tokenUsage?: { input: number; output: number };
  lastStatus: string;
  sandbox?: SandboxSummary;
}

export interface SandboxSummary {
  status: "not-started" | "running" | "stopped" | "closed" | "unsafe-local";
  backend: "docker" | "unsafe-local";
  sandboxId: string | null;
  image: string | null;
  network: string;
  workspaceSecretFilesVisible: string[];
}

export interface PendingApproval {
  id: string;
  kind: "tool" | "plan";
  message: string;
  choices: string[];
  createdAt: string;
}

export interface SessionDetail extends SessionSummary {
  messages: ChatMessage[];
  pendingApprovals: PendingApproval[];
  hasTrace: boolean;
  lastEventSeq: number;
}

export interface ChatMessage {
  id?: string;
  role: "user" | "assistant";
  content: string;
  turnId?: string | null;
  status?: string;
}

export interface AppConfig {
  model: string;
  protocol: string;
  cwd: string;
  permissionMode: string;
  runtimeStatus: string;
}

export interface TraceNode {
  id: string;
  event: RuntimeEvent;
  children: TraceNode[];
  firstSeq: number;
}
