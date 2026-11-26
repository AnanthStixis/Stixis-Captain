// Runs the companion's real client code against a running gateway:
//   npx esbuild scripts/live-check.mts --bundle --platform=node --format=esm --outfile=live-check.mjs
//   node live-check.mjs <key-file>
// (development check; the extension itself runs inside VS Code)
import { readFileSync } from "node:fs";

import { describe, fetchContinueConfig, fetchStatus } from "../src/core";

const key = readFileSync(process.argv[2] ?? "", "utf8").trim();
const url = process.env.CAPTAIN_GATEWAY ?? "http://127.0.0.1:8000";
const http = (u: string, init: { headers: Record<string, string>; signal: AbortSignal }) => fetch(u, init);

const ok = await fetchStatus(http, url, key);
console.log("with key   :", describe(ok).text);
console.log("bad key    :", describe(await fetchStatus(http, url, "cpt_wrong")).text);
console.log("no key     :", describe(await fetchStatus(http, url, undefined)).text);
console.log("wrong port :", describe(await fetchStatus(http, "http://127.0.0.1:9", key, 1500)).text);
const cfg = await fetchContinueConfig(http, url, key);
console.log("continue   :", cfg.ok ? cfg.yaml.split("\n").slice(0, 6).join(" | ") : cfg.reason);
