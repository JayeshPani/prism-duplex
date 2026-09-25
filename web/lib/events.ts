// Mirrors agent/coordinator/events.py. Every coordinator decision arrives as
// one of these over the LiveKit data channel (topic "agent-events").

export type AgentEvent = {
  type: string;
  ts: number; // unix seconds (agent clock)
  data: Record<string, any>;
  snapshot?: { slots: Record<string, any>; committed: { tool: string; args: any; result: any }[]; pending: string };
};

export type Turn =
  | { kind: "user"; text: string; ts: number; corrections: { slot: string; from: string; to: string }[] }
  | { kind: "agent"; text: string; ts: number; filler: boolean };

// Build the conversation shown in the transcript. Segments merged by the
// coordinator (the user paused, then kept going) collapse into one bubble.
export function buildTurns(events: AgentEvent[]): Turn[] {
  const turns: Turn[] = [];
  let open: Extract<Turn, { kind: "user" }> | null = null;
  for (const e of events) {
    if (e.type === "user_final") {
      if (open) open.text = e.data.merged;
      else {
        open = { kind: "user", text: e.data.merged ?? e.data.text, ts: e.ts, corrections: [] };
        turns.push(open);
      }
    } else if (e.type === "plan_ready" && open) {
      open.corrections = (e.data.corrections ?? []).filter((c: any) => c.from && c.to);
    } else if (e.type === "gate_committed") {
      open = null;
    } else if (e.type === "agent_say") {
      turns.push({ kind: "agent", text: e.data.text, ts: e.ts, filler: false });
    } else if (e.type === "ack") {
      // the agent_say that follows carries the same text; it is dropped below
      turns.push({ kind: "agent", text: e.data.text, ts: e.ts, filler: true });
    }
  }
  // drop the agent_say that duplicates each filler
  return turns.filter((t, i) => !(t.kind === "agent" && !t.filler && i > 0 &&
    turns[i - 1].kind === "agent" && (turns[i - 1] as any).filler && turns[i - 1].text === t.text));
}

// Spans for the time ribbon.
export type Span = { lane: "you" | "agent" | "gate" | "tools"; start: number; end: number | null;
  tone: "neutral" | "held" | "done" | "stale" | "blocked"; label: string };

export function buildSpans(events: AgentEvent[]): Span[] {
  const spans: Span[] = [];
  const openLane: Record<string, Span | undefined> = {};
  const tools: Record<string, Span> = {};
  const close = (lane: string, t: number) => { const s = openLane[lane]; if (s && s.end === null) s.end = t; openLane[lane] = undefined; };

  for (const e of events) {
    const t = e.ts;
    switch (e.type) {
      case "user_state":
        if (e.data.state === "speaking") { close("you", t); openLane.you = { lane: "you", start: t, end: null, tone: "neutral", label: "speaking" }; spans.push(openLane.you); }
        else close("you", t);
        break;
      case "agent_state":
        if (e.data.state === "speaking") { close("agent", t); openLane.agent = { lane: "agent", start: t, end: null, tone: "done", label: "speaking" }; spans.push(openLane.agent); }
        else close("agent", t);
        break;
      case "gate_open": {
        close("gate", t);
        const s: Span = { lane: "gate", start: t, end: null, tone: "held", label: `holding ${e.data.hold_ms} ms` };
        openLane.gate = s; spans.push(s);
        break;
      }
      case "gate_cancelled": {
        const s = openLane.gate;
        if (s) { s.end = t; s.tone = "stale"; s.label = "plan dropped: user kept talking"; openLane.gate = undefined; }
        else spans.push({ lane: "gate", start: t, end: t + 0.25, tone: "stale", label: e.data.reason });
        break;
      }
      case "gate_committed": {
        const s = openLane.gate;
        if (s) { s.end = t; s.tone = "done"; s.label = "committed"; openLane.gate = undefined; }
        break;
      }
      case "tool_started":
        // call ids restart at c1 each turn; the latest span for an id is the live one
        tools[e.data.id] = { lane: "tools", start: t, end: null, tone: "neutral", label: e.data.tool };
        spans.push(tools[e.data.id]);
        break;
      case "tool_done": case "tool_cancelled": case "tool_error": {
        const s = tools[e.data.id];
        if (s && s.end === null) {
          s.end = t;
          s.tone = e.type === "tool_done" ? "done" : "stale";
          if (e.type === "tool_cancelled") s.label = `${e.data.tool} cancelled`;
        }
        break;
      }
      case "tool_blocked":
        spans.push({ lane: "tools", start: t, end: t + 0.35, tone: "blocked", label: `${e.data.tool} blocked: already done` });
        break;
    }
  }
  return spans;
}
