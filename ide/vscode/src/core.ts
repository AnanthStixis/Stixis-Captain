// Pure logic for the Captain companion: no VS Code imports, so it is unit-tested directly.

export interface GovernanceStatus {
  principal: string;
  register_id: string;
  system: string;
  risk_tier: string;
  autonomy: string;
  project: string;
  application: string;
  environment: string;
  allowed_models: string[];
}

export type StatusResult =
  | { kind: "no-key" }
  | { kind: "bad-url"; reason: string }
  | { kind: "rejected" }
  | { kind: "unreachable" }
  | { kind: "error"; status: number }
  | { kind: "ok"; status: GovernanceStatus };

export type FetchLike = (url: string, init: { headers: Record<string, string>; signal: AbortSignal }) => Promise<{
  ok: boolean;
  status: number;
  json(): Promise<unknown>;
  text(): Promise<string>;
}>;

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"]);

/** The key is a bearer secret: never send it over plain http to another machine, unless
 * `allowPlainHttp` is on (test networks only, setting captain.allowPlainHttp). */
export function checkGatewayUrl(raw: string, allowPlainHttp = false): { ok: true; base: string } | { ok: false; reason: string } {
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return { ok: false, reason: "The gateway URL is not a valid URL." };
  }
  if (u.username || u.password) return { ok: false, reason: "The gateway URL must not contain credentials." };
  if (u.protocol === "https:" || (u.protocol === "http:" && (LOOPBACK.has(u.hostname) || allowPlainHttp))) {
    return { ok: true, base: u.origin + u.pathname.replace(/\/+$/, "") };
  }
  if (u.protocol === "http:")
    return { ok: false, reason: "Use https for the gateway. On a test network you may allow plain http with the setting captain.allowPlainHttp." };
  return { ok: false, reason: "Use https for the gateway (plain http is only allowed for this machine)." };
}

function isStatus(v: unknown): v is GovernanceStatus {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  return (
    typeof o.principal === "string" &&
    typeof o.register_id === "string" &&
    typeof o.risk_tier === "string" &&
    Array.isArray(o.allowed_models)
  );
}

async function get(f: FetchLike, url: string, key: string, timeoutMs: number) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    return await f(url, { headers: { Authorization: `Bearer ${key}` }, signal: ctl.signal });
  } finally {
    clearTimeout(timer);
  }
}

export async function fetchStatus(
  f: FetchLike,
  gatewayUrl: string,
  key: string | undefined,
  timeoutMs = 5000,
  allowPlainHttp = false,
): Promise<StatusResult> {
  if (!key) return { kind: "no-key" };
  const chk = checkGatewayUrl(gatewayUrl, allowPlainHttp);
  if (!chk.ok) return { kind: "bad-url", reason: chk.reason };
  try {
    const r = await get(f, `${chk.base}/v1/governance/status`, key, timeoutMs);
    if (r.status === 401) return { kind: "rejected" };
    if (!r.ok) return { kind: "error", status: r.status };
    const body = await r.json();
    return isStatus(body) ? { kind: "ok", status: body } : { kind: "error", status: r.status };
  } catch {
    return { kind: "unreachable" };
  }
}

/** The generated Continue configuration (the key is left as a placeholder by the gateway). */
export async function fetchContinueConfig(
  f: FetchLike,
  gatewayUrl: string,
  key: string,
  timeoutMs = 5000,
  allowPlainHttp = false,
): Promise<{ ok: true; yaml: string } | { ok: false; reason: string }> {
  const chk = checkGatewayUrl(gatewayUrl, allowPlainHttp);
  if (!chk.ok) return { ok: false, reason: chk.reason };
  try {
    const r = await get(f, `${chk.base}/v1/ide/continue`, key, timeoutMs);
    if (r.status === 401) return { ok: false, reason: "The gateway rejected the key." };
    if (!r.ok) return { ok: false, reason: `The gateway answered HTTP ${r.status}.` };
    return { ok: true, yaml: await r.text() };
  } catch {
    return { ok: false, reason: "The gateway could not be reached." };
  }
}

export interface Display {
  text: string;
  tooltip: string;
  level: "ok" | "warn" | "error";
}

const SCOPE_NOTE =
  "Covers AI calls this key makes through Captain. It does not watch other AI tools on this machine.";

/** What the status bar says. It never claims more than the key being valid. */
export function describe(r: StatusResult): Display {
  switch (r.kind) {
    case "no-key":
      return { text: "Captain: not signed in", tooltip: "Run “Captain: Set Key” to connect this editor.", level: "warn" };
    case "bad-url":
      return { text: "Captain: check gateway URL", tooltip: r.reason, level: "error" };
    case "rejected":
      return { text: "Captain: key rejected", tooltip: "The gateway did not accept this key. Ask for a new one.", level: "error" };
    case "unreachable":
      return { text: "Captain: gateway unreachable", tooltip: "AI calls through Captain will fail until it is reachable.", level: "error" };
    case "error":
      return { text: "Captain: gateway error", tooltip: `The gateway answered HTTP ${r.status}.`, level: "error" };
    case "ok": {
      const s = r.status;
      const tier = s.risk_tier.replace("TIER_", "Tier ");
      return {
        text: `Captain: ${s.register_id} · ${tier}`,
        tooltip: [
          `${s.system} (${s.register_id})`,
          `Signed in as ${s.principal}`,
          `${tier}, autonomy ${s.autonomy}, ${s.environment.toLowerCase()}`,
          `Models: ${s.allowed_models.join(", ") || "none"}`,
          SCOPE_NOTE,
        ].join("\n"),
        level: "ok",
      };
    }
  }
}
