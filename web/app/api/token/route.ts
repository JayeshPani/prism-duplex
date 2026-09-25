import { AccessToken } from "livekit-server-sdk";
import { NextResponse } from "next/server";

// Mints a short-lived LiveKit token for the browser. The agent worker auto-joins
// every new room; rooms named "car-*" run the in-car extension.
export async function GET(req: Request) {
  const mode = new URL(req.url).searchParams.get("mode") === "car" ? "car" : "web";
  const { LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET } = process.env;
  if (!LIVEKIT_URL || !LIVEKIT_API_KEY || !LIVEKIT_API_SECRET) {
    return NextResponse.json({ error: "LiveKit keys are missing from .env.local" }, { status: 500 });
  }
  const room = `${mode}-${Math.random().toString(36).slice(2, 8)}`;
  const at = new AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET, { identity: `driver-${Date.now()}`, ttl: "30m" });
  at.addGrant({ room, roomJoin: true, canPublish: true, canSubscribe: true });
  return NextResponse.json({ url: LIVEKIT_URL, token: await at.toJwt(), room });
}
