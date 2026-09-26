"use client";

import { useEffect, useState } from "react";
import type { Span } from "@/lib/events";

const LANES: { id: Span["lane"]; label: string }[] = [
  { id: "you", label: "You" },
  { id: "agent", label: "Agent" },
  { id: "gate", label: "Commit gate" },
  { id: "tools", label: "Tools" },
];
const TONE: Record<Span["tone"], string> = {
  neutral: "#8a95a5", held: "var(--held)", done: "var(--commit)", stale: "var(--stale)", blocked: "var(--stale)", error: "#b42318", unknown: "#8a5600", reused: "#58728c",
};
const WINDOW_S = 24;
const W = 1200, LANE_H = 30, LEFT = 110, TOP = 6;

// One shared clock for speech, the commit gate and tool calls: a stale plan is
// visible as an amber hold cut short in rose; a blocked duplicate as a hollow mark.
export default function Ribbon({ spans, clockOffset, reviewEnd, reviewStart }: { spans: Span[]; clockOffset: number; reviewEnd?: number; reviewStart?: number }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (reviewEnd != null) return;
    let raf = 0;
    const tick = () => { setNow(Date.now() / 1000); raf = requestAnimationFrame(tick); };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [reviewEnd]);

  const agentNow = reviewEnd ?? now + clockOffset;
  const windowS = reviewStart != null ? Math.max(1, agentNow - reviewStart + 0.1) : WINDOW_S;
  const t0 = agentNow - windowS;
  const x = (t: number) => LEFT + ((t - t0) / windowS) * (W - LEFT);
  const H = TOP + LANES.length * LANE_H + 18;

  return (
    <section className="ribbon" aria-label="Timeline of speech, commit gate and tool calls">
      <svg viewBox={`0 0 ${W} ${H}`} role="img">
        {LANES.map((l, i) => (
          <g key={l.id}>
            <text x={0} y={TOP + i * LANE_H + 19} fontSize="13" fill="var(--muted)">{l.label}</text>
            <line x1={LEFT} x2={W} y1={TOP + i * LANE_H + 15} y2={TOP + i * LANE_H + 15} stroke="var(--rule)" />
          </g>
        ))}
        {[0, 1, 2, 3, 4].map(i => i * windowS / 5).map((s) => (
          <text key={s} x={x(agentNow - s)} y={H - 2} fontSize="11" fill="var(--muted)" textAnchor="middle">
            {s === 0 ? "now" : `-${Number(s.toFixed(1))}s`}
          </text>
        ))}
        {spans.filter((s) => (s.end ?? agentNow) > t0).map((s, i) => {
          const lane = LANES.findIndex((l) => l.id === s.lane);
          const x1 = Math.max(LEFT, x(s.start));
          const x2 = Math.max(x1 + 3, x(s.end ?? agentNow));
          const y = TOP + lane * LANE_H + 6;
          const hollow = s.tone === "blocked";
          return (
            <g key={i}>
              <title>{s.label}</title>
              <rect x={x1} y={y} width={x2 - x1} height={18} rx={4}
                fill={hollow ? "none" : TONE[s.tone]} stroke={TONE[s.tone]} strokeWidth={hollow ? 2 : 0}
                opacity={s.tone === "neutral" && s.lane === "you" ? 0.55 : 1} />
              {x2 - x1 > 70 && (
                <text x={x1 + 7} y={y + 13} fontSize="12" fill={hollow ? "var(--stale)" : "white"}>
                  {s.label}
                </text>
              )}
            </g>
          );
        })}
        <line x1={x(agentNow)} x2={x(agentNow)} y1={0} y2={H - 14} stroke="var(--ink)" strokeWidth={1} />
      </svg>
      <div className="legend">
        <span style={{ ["--c" as any]: "var(--held)" }}>Plan held, waiting for you to finish</span>
        <span style={{ ["--c" as any]: "var(--commit)" }}>Committed or done</span>
        <span style={{ ["--c" as any]: "var(--stale)" }}>Cancelled or retry blocked</span>
        <span style={{ ["--c" as any]: "#b42318" }}>Failed</span>
        <span style={{ ["--c" as any]: "#8a5600" }}>Outcome unknown</span>
        <span style={{ ["--c" as any]: "#58728c" }}>Reused result</span>
      </div>
    </section>
  );
}
