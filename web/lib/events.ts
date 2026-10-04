// Mirrors agent/coordinator/events.py. Every coordinator decision arrives as
// one of these over the LiveKit data channel (topic "agent-events").

export type AgentEvent = {
  type: string;
  ts: number; // unix seconds (agent clock)
  data: Record<string, any>;
  snapshot?: { slots: Record<string, any>; committed: { tool: string; args: any; result: any; status?: string }[]; pending: string };
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
  tone: "neutral" | "held" | "done" | "stale" | "blocked" | "error" | "unknown" | "reused"; label: string };

export function buildSpans(events: AgentEvent[]): Span[] {
  const spans: Span[] = [];
  const openLane: Record<string, Span | undefined> = {};
  const tools: Record<string, Span> = {};
  const close = (lane: string, t: number) => { const s = openLane[lane]; if (s && s.end === null) s.end = t; openLane[lane] = undefined; };

  for (const e of events) {
    const t = e.ts;
    const callKey = `${e.data.execution_id ?? "legacy"}:${e.data.id}`;
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
      case "tool_waiting":
        tools[callKey] = { lane: "tools", start: t, end: null, tone: "held", label: `${e.data.tool} waiting` };
        spans.push(tools[callKey]);
        break;
      case "tool_started":
        if (tools[callKey]?.end === null) tools[callKey].end = t;
        // The planner restarts call IDs each turn; execution identity disambiguates them.
        tools[callKey] = { lane: "tools", start: t, end: null, tone: "neutral", label: e.data.tool };
        spans.push(tools[callKey]);
        break;
      case "tool_done": case "tool_cancelled": case "tool_error": case "tool_unknown": {
        const s = tools[callKey];
        if (s && s.end === null) {
          s.end = t;
          s.tone = e.type === "tool_done" ? "done" : e.type === "tool_error" ? "error" : e.type === "tool_unknown" ? "unknown" : "stale";
          s.label = `${e.data.tool} ${e.type === "tool_done" ? "succeeded" : e.type === "tool_unknown" ? "outcome unknown" : e.type === "tool_error" ? "failed" : "cancelled"}`;
        }
        else if (e.type === "tool_error" || e.type === "tool_unknown")
          spans.push({ lane: "tools", start: t, end: t + 0.35, tone: e.type === "tool_error" ? "error" : "unknown", label: `${e.data.tool}: ${e.data.error ?? "outcome unknown"}` });
        break;
      }
      case "tool_reused":
        if (tools[callKey]?.end === null) tools[callKey].end = t;
        spans.push({ lane: "tools", start: t, end: t + 0.35, tone: "reused", label: `${e.data.tool} reused` });
        break;
      case "tool_blocked":
        if (tools[callKey]?.end === null) tools[callKey].end = t;
        spans.push({ lane: "tools", start: t, end: t + 0.35, tone: "blocked", label: `${e.data.tool} blocked: already done` });
        break;
    }
  }
  return spans;
}


export type NavView = { polyline: [number, number][]; destination?: string; eta?: number; stops: string[] };

export function buildNavigation(events: AgentEvent[]): NavView | null {
  let nav: NavView | null = null;
  let version = -1;
  for (const e of events) {
    if (e.type !== "tool_done" || e.data.result?.status !== "success") continue;
    if (!["start_navigation", "add_waypoint", "cancel_navigation"].includes(e.data.tool)) continue;
    const r = e.data.result;
    if (r.navigation_version != null) {
      if (r.navigation_version < version) continue;
      version = r.navigation_version;
      nav = r.route_active ? { polyline: r.polyline, destination: r.destination,
        eta: r.eta_min, stops: r.stops ?? [] } : null;
    } else if (e.data.tool === "cancel_navigation") nav = null;
    else if (e.data.tool === "start_navigation")
      nav = { polyline: r.polyline, destination: r.navigating_to, eta: r.eta_min, stops: r.stops ?? [] };
    else if (nav) {
      const previous = nav as NavView;
      nav = { ...previous, polyline: r.polyline, eta: r.eta_min, stops: r.stops ?? [...previous.stops, r.added] };
    }
  }
  return nav;
}


export function parseTrace(text: string): AgentEvent[] {
  const rows = text.split(/\r?\n/).filter(line => line.trim()).map(line => JSON.parse(line));
  const rooms = new Set(rows.filter(r => r.kind === "coordinator_event").map(r => r.room));
  if (rooms.size > 1) throw new Error("Review a trace containing a single room.");
  const events = rows.flatMap(row => row.kind ? (row.kind === "coordinator_event" ? [row.event] : []) : [row]);
  if (!events.length || events.some(e => !e || typeof e.type !== "string" || !Number.isFinite(e.ts) || !e.data || typeof e.data !== "object"))
    throw new Error("Expected coordinator JSONL events with type, ts and data fields.");
  return events;
}
