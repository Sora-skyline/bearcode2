import { describe, expect, it } from "vitest";
import { buildTrace, mergeEvent } from "./events";
import type { RuntimeEvent } from "./types";

const event = (overrides: Partial<RuntimeEvent>): RuntimeEvent => ({
  schemaVersion: "1.0", id: "1", seq: 1, timestamp: "2026-01-01T00:00:00Z",
  sessionId: "s", turnId: "t", spanId: "span", parentSpanId: null, agentId: "main",
  type: "turn.started", status: "running", durationMs: null, payload: {}, ...overrides,
});

describe("runtime event reducer", () => {
  it("merges text deltas by span", () => {
    const first = event({ type: "assistant.delta", payload: { text: "Bear" } });
    const second = event({ id: "2", seq: 2, type: "assistant.delta", payload: { text: " Code" } });
    expect(mergeEvent(mergeEvent([], first), second)[0].payload.text).toBe("Bear Code");
  });

  it("builds model and tool parent relationships", () => {
    const turn = event({ spanId: "turn" });
    const model = event({ id: "2", seq: 2, type: "model.completed", status: "completed", spanId: "model", parentSpanId: "turn" });
    const tool = event({ id: "3", seq: 3, type: "tool.completed", status: "completed", spanId: "tool", parentSpanId: "model" });
    const trace = buildTrace([turn, model, tool]);
    expect(trace[0].children[0].children[0].id).toBe("tool");
  });

  it("keeps out-of-order events sorted without reordering normal appends", () => {
    const first = event({ id: "1", seq: 1 });
    const third = event({ id: "3", seq: 3 });
    const second = event({ id: "2", seq: 2 });
    expect(mergeEvent(mergeEvent(mergeEvent([], first), third), second).map((item) => item.seq)).toEqual([1, 2, 3]);
  });
});
