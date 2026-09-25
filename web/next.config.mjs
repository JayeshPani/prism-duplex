import fs from "node:fs";
import path from "node:path";

// Share the repo-root .env.local (LiveKit keys) with the web app; keys stay server-side.
const envFile = path.resolve(process.cwd(), "..", ".env.local");
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
  }
}

/** @type {import('next').NextConfig} */
export default { reactStrictMode: false };
