import assert from "node:assert/strict";
import test from "node:test";
import { buildSpans, buildNavigation, parseTrace, type AgentEvent } from "./events.ts";

const ev = (type: string, ts: number, data: Record<string, any>): AgentEvent => ({ type, ts, data });

test("overlapping repeated call IDs retain distinct spans", () => {
  const spans = buildSpans([
    ev("tool_started", 1, { id: "c1", execution_id: "old", tool: "search" }),
    ev("tool_started", 2, { id: "c1", execution_id: "new", tool: "search" }),
    ev("tool_cancelled", 3, { id: "c1", execution_id: "old", tool: "search" }),
    ev("tool_done", 4, { id: "c1", execution_id: "new", tool: "search" }),
  ]);
  assert.deepEqual(spans.map(s => [s.start, s.end, s.tone]), [[1, 3, "stale"], [2, 4, "done"]]);
});

test("cancelled navigation cannot be restored by a late older result", () => {
  const events = [
    ev("tool_done", 1, { tool: "start_navigation", result: { status: "success", navigation_version: 1,
      route_active: true, destination: "Office", polyline: [[1, 2], [3, 4]], stops: [] } }),
    ev("tool_done", 2, { tool: "cancel_navigation", result: { status: "success", navigation_version: 2, route_active: false } }),
  ];
  assert.equal(buildNavigation([...events, events[0]]), null);
});

test("failed cancellation leaves actual navigation visible", () => {
  const nav = buildNavigation([
    ev("tool_done", 1, { tool: "start_navigation", result: { status: "success", navigation_version: 1,
      route_active: true, destination: "Office", polyline: [[1, 2], [3, 4]], stops: [] } }),
    ev("tool_done", 2, { tool: "cancel_navigation", result: { status: "error" } }),
  ]);
  assert.equal(nav?.destination, "Office");
});

test("unknown outcomes and reused results remain visible", () => {
  const spans = buildSpans([
    ev("tool_unknown", 1, { tool: "write", error: "connection lost" }),
    ev("tool_reused", 2, { tool: "lookup" }),
  ]);
  assert.deepEqual(spans.map(s => s.tone), ["unknown", "reused"]);
});


test("development trace wrapper imports coordinator events while retaining room isolation", () => {
  const row = { kind: "coordinator_event", room: "a", event: ev("user_final", 1, { text: "hello" }) };
  assert.deepEqual(parseTrace(JSON.stringify(row) + "\n" + JSON.stringify({ kind: "backend_started", room: "a" })), [row.event]);
  assert.throws(() => parseTrace(JSON.stringify(row) + "\n" + JSON.stringify({ ...row, room: "b" })), /single room/);
});


test("cached and blocked calls close their waiting spans without dispatch", () => {
  for (const outcome of ["tool_reused", "tool_blocked"]) {
    const data = { execution_id: "one", id: "c1", tool: "lookup" };
    const spans = buildSpans([ev("tool_waiting", 1, data), ev(outcome, 2, data)]);
    assert.equal(spans[0].end, 2);
    assert.equal(spans.filter(span => span.end === null).length, 0);
  }
});
