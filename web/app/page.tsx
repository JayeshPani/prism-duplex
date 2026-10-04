"use client";

import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { Room, RoomEvent, Track } from "livekit-client";
import dynamic from "next/dynamic";
import Ribbon from "@/components/Ribbon";
import { buildSpans, buildTurns, buildNavigation, parseTrace, type AgentEvent, type Turn } from "@/lib/events";

const CarMap = dynamic(() => import("@/components/CarMap"), { ssr: false });
type Mode = "assistant" | "car";

function UserLine({ turn }: { turn: Extract<Turn, { kind: "user" }> }) {
  // Show each self-correction in place: the dropped value struck out, the kept one after it.
  let parts: ReactNode[] = [turn.text];
  turn.corrections.forEach((c, ci) => {
    parts = parts.flatMap((p): ReactNode[] => {
      if (typeof p !== "string") return [p];
      const i = p.toLowerCase().indexOf(String(c.from).toLowerCase());
      if (i < 0) return [p];
      return [p.slice(0, i), <del key={`d${ci}`}>{p.slice(i, i + String(c.from).length)}</del>, p.slice(i + String(c.from).length)];
    });
  });
  const kept = turn.corrections.map((c) => c.to).filter(Boolean);
  return (
    <p className="say user">
      {parts}
      {kept.length > 0 && <> {" "}<ins>→ {kept.join(", ")}</ins></>}
    </p>
  );
}

export default function Page() {
  const [mode, setMode] = useState<Mode>("car");
  const [status, setStatus] = useState<"idle" | "connecting" | "live">("idle");
  const [error, setError] = useState<string | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [reviewName, setReviewName] = useState<string | null>(null);
  const traceInput = useRef<HTMLInputElement | null>(null);
  const [offset, setOffset] = useState(0);
  const room = useRef<Room | null>(null);

  const start = useCallback(async () => {
    setError(null); setEvents([]); setOffset(0); setReviewName(null); setStatus("connecting");
    try {
      const res = await fetch(`/api/token?mode=${mode === "car" ? "car" : "assistant"}`);
      const { url, token, error } = await res.json();
      if (error) throw new Error(error);
      const r = new Room({ adaptiveStream: true });
      r.on(RoomEvent.DataReceived, (payload, _p, _k, topic) => {
        if (topic !== "agent-events") return;
        const ev: AgentEvent = JSON.parse(new TextDecoder().decode(payload));
        setOffset((o) => (o === 0 ? ev.ts - Date.now() / 1000 : Math.min(o, ev.ts - Date.now() / 1000)));
        setEvents((prev) => [...prev, ev]);
      });
      room.current = r;
      r.on(RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((el) => el.remove()));
      r.on(RoomEvent.TrackSubscribed, (track) => {
        if (track.kind === Track.Kind.Audio) document.body.appendChild(track.attach());
      });
      r.on(RoomEvent.Disconnected, () => setStatus("idle"));
      await r.connect(url, token);
      await r.localParticipant.setMicrophoneEnabled(true);
      room.current = r;
      setStatus("live");
    } catch (e: any) {
      await room.current?.disconnect();
      room.current = null;
      setError(`Could not connect: ${e.message ?? e}. Check that the agent worker is running.`);
      setStatus("idle");
    }
  }, [mode]);

  const stop = useCallback(async () => { await room.current?.disconnect(); room.current = null; setStatus("idle"); }, []);

  const turns = useMemo(() => buildTurns(events), [events]);
  const spans = useMemo(() => buildSpans(events), [events]);
  const snapshot = [...events].reverse().find((e) => e.snapshot)?.snapshot;

  const slotHistory = useMemo(() => {
    const h: Record<string, { value: any; previous: any[] }> = {};
    for (const e of events) if (e.type === "slot_update" && e.data.slot) {
      const cur = h[e.data.slot] ?? { value: undefined, previous: [] };
      if (e.data.previous != null && !cur.previous.includes(e.data.previous)) cur.previous.push(e.data.previous);
      cur.value = e.data.value;
      h[e.data.slot] = cur;
    }
    return h;
  }, [events]);

  const nav = useMemo(() => buildNavigation(events), [events]);

  const reviewTrace = async (file: File) => {
    try {
      const imported = parseTrace(await file.text());
      setEvents(imported); setReviewName(file.name); setOffset(0); setError(null);
      setMode(imported.some(e => e.data.tool === "start_navigation") ? "car" : "assistant");
    } catch (e: any) { setError(`Could not read trace: ${e.message ?? e}`); }
  };

  const live = status === "live";
  return (
    <main className="shell">
      <header className="bar">
        <h1>PRISM Duplex</h1>
        <span className="status" aria-live="polite">
          {status === "live" ? "Listening. Interrupt or correct yourself any time." : status === "connecting" ? "Connecting…" : "Not connected"}
        </span>
        <div className="spacer" />
        <div className="modes" role="group" aria-label="Use case">
          <button aria-pressed={mode === "car"} disabled={live} onClick={() => setMode("car")}>In-car</button>
          <button aria-pressed={mode === "assistant"} disabled={live} onClick={() => setMode("assistant")}>Assistant</button>
        </div>
        <button className={`talk ${live ? "live" : ""}`} onClick={live ? stop : start} disabled={status === "connecting"}>
          {live ? "End conversation" : "Start talking"}
        </button>
      </header>

      <div className="trace-review">
        <input ref={traceInput} type="file" accept=".jsonl" hidden onChange={e => {
          const file = e.target.files?.[0]; if (file) void reviewTrace(file); e.target.value = "";
        }} />
        <button disabled={status !== "idle"} onClick={() => traceInput.current?.click()}>Review a saved trace</button>
        {reviewName && <span>Trace review: {reviewName}. Recorded events; no live audio.</span>}
      </div>

      <div className="main">
        <section className="pane" aria-label="Conversation">
          <h2>Conversation</h2>
          <div className="transcript" aria-live="polite">
            {error && <p className="empty" role="alert">{error}</p>}
            {!error && turns.length === 0 && (
              <p className="empty">
                {mode === "car"
                  ? "Try: “Take me to the airport… actually, the office first.” Then, while it answers: “add a coffee stop.”"
                  : "Try: “Find flights to Lisbon on June 12th, no wait, the 14th, and book it for Priya Nair.”"}
              </p>
            )}
            {turns.map((t, i) => t.kind === "user"
              ? <UserLine key={i} turn={t} />
              : <p key={i} className={`say agent ${t.filler ? "filler" : ""}`}>{t.text}</p>)}
          </div>
        </section>

        <section className="pane" aria-label={mode === "car" ? "Navigation" : "What the agent knows"}>
          {mode === "car" ? (
            <>
              <h2>Navigation</h2>
              <CarMap nav={nav} />
            </>
          ) : (
            <>
              <h2>What the agent knows</h2>
              <div className="state">
                <div>
                  <h3>Details from you</h3>
                  {Object.keys(slotHistory).length === 0 ? <p className="empty">Nothing yet.</p> : (
                    <dl className="slots">
                      {Object.entries(slotHistory).map(([k, v]) => (
                        <div key={k} style={{ display: "contents" }}>
                          <dt>{k.replace(/_/g, " ")}</dt>
                          <dd>{v.previous.filter((p) => p !== v.value).map((p, i) => <s key={i}>{String(p)}</s>)}{String(v.value)}</dd>
                        </div>
                      ))}
                    </dl>
                  )}
                </div>
                <div>
                  <h3>Action outcomes</h3>
                  {!snapshot?.committed?.length ? <p className="empty">No changes made yet.</p> : (
                    <ul className="ledger">
                      {snapshot.committed.map((c, i) => (
                        <li key={i}><code>{c.tool.replace(/_/g, " ")}</code> {Object.values(c.args).join(", ")} — {c.status === "unknown" ? "outcome unknown" : "completed"}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            </>
          )}
        </section>
      </div>

      <Ribbon spans={spans} clockOffset={offset} reviewEnd={reviewName ? events.reduce((end, e) => Math.max(end, e.ts), 0) + 0.1 : undefined} reviewStart={reviewName ? events.reduce((start, e) => Math.min(start, e.ts), Infinity) : undefined} />
    </main>
  );
}
