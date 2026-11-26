// Typed client for the Captain Admin API. Two ways to be signed in:
//  - SSO (production): an HttpOnly session cookie set by the server. JavaScript never
//    sees an identity token. State-changing requests carry the session's CSRF token,
//    held in memory only.
//  - Dev token (local only): kept in sessionStorage (cleared when the tab closes).
// Every permission is enforced by the server; the UI only hides what the user cannot do.

const TOKEN_KEY = "captain.admin.token";
let csrfToken: string | null = null;

export function setCsrf(t: string | null): void {
  csrfToken = t;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(t: string | null): void {
  try {
    if (t) sessionStorage.setItem(TOKEN_KEY, t);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: the user signs in again */
  }
}

function authHeaders(method: string): Record<string, string> {
  const token = getToken();
  if (token) return { Authorization: `Bearer ${token}` };
  return method !== "GET" && csrfToken ? { "X-CSRF-Token": csrfToken } : {};
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/admin/v1${path}`, {
    method,
    credentials: "same-origin", // sends the session cookie
    headers: {
      ...authHeaders(method),
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let code = "ERROR";
    let message = res.statusText;
    try {
      const j = await res.json();
      const e = j.detail ?? j;
      code = e.error ?? code;
      message = e.message ?? (typeof e.detail === "string" ? e.detail : JSON.stringify(e));
    } catch {
      /* non-JSON error body */
    }
    // 401 = not signed in. A 403 (permission or CSRF) never ends the session.
    if (res.status === 401) {
      setToken(null);
      setCsrf(null);
    }
    throw new ApiError(res.status, code, message);
  }
  return (await res.json()) as T;
}

export const api = {
  get: <T>(p: string) => request<T>("GET", p),
  post: <T>(p: string, b?: unknown) => request<T>("POST", p, b ?? {}),
  put: <T>(p: string, b: unknown) => request<T>("PUT", p, b),
  del: <T>(p: string) => request<T>("DELETE", p),
};

/** Authenticated file download (evidence zip, XLSX, CSV). */
/** Plain text (for example a guide in markdown). */
export async function getText(path: string): Promise<string> {
  const res = await fetch(`/admin/v1${path}`, { credentials: "same-origin", headers: authHeaders("GET") });
  if (!res.ok) throw new ApiError(res.status, "ERROR", `could not load (${res.status})`);
  return res.text();
}

export async function download(path: string, fallbackName: string): Promise<void> {
  const res = await fetch(`/admin/v1${path}`, { credentials: "same-origin", headers: authHeaders("GET") });
  if (!res.ok) {
    let message = `download failed (${res.status})`;
    try {
      const j = await res.json();
      message = (j.detail ?? j).message ?? message;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, "DOWNLOAD_FAILED", message);
  }
  const blob = await res.blob();
  const cd = res.headers.get("Content-Disposition") ?? "";
  const name = /filename="([^"]+)"/.exec(cd)?.[1] ?? fallbackName;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

// ------------------------------------------------------------------ types
export interface Me {
  id: string;
  roles: string[];
  permissions: string[];
}

export interface SessionInfo extends Me {
  csrf_token: string;
}

export interface AuthConfig {
  sso: boolean;
  dev_token: boolean;
  stixis_console?: boolean;
  email_link?: boolean;
}

export interface Overview {
  period_days: number;
  governed_calls: number;
  observed_events: number;
  decisions: Record<string, number>;
  refusals_by_reason: Record<string, number>;
  risk_levels: Record<string, number>;
  spend_micros: number;
  estimated_spend_micros: number;
  tokens: { input: number; output: number };
  latency_ms: { p50: number | null; p95: number | null };
  spend_by_model: Record<string, number>;
  daily: { day: string; calls: number; refused: number; spend_micros: number }[];
  approvals_pending: number;
  alerts_recent: Alert[];
  kill_switches_active: number;
  definitions: Record<string, string>;
}

export interface UsageBreakdown {
  by: string;
  period_days: number;
  total_cost_micros: number;
  items: {
    key: string;
    calls: number;
    refused: number;
    tokens_in: number;
    tokens_out: number;
    cost_micros: number;
    estimated_cost_micros: number;
    share_pct: number;
  }[];
}

export interface ObservedSource {
  source: string;
  kind: "vendor import" | "hook";
  events: number;
  unmapped_events: number;
  principals: number;
  tokens_in: number;
  tokens_out: number;
  cost_micros: number;
  first_day: string;
  last_day: string;
}

export interface ObservedActor {
  source: string;
  principal: string;
  mapped: boolean;
  events: number;
  tokens_in: number;
  tokens_out: number;
  cost_micros: number;
}

export interface ObservedUsage {
  period_days: number;
  totals: {
    events: number;
    sources: number;
    principals: number;
    unmapped_events: number;
    tokens_in: number;
    tokens_out: number;
    cost_micros: number;
  };
  governed_spend_micros: number;
  by_source: ObservedSource[];
  actors: ObservedActor[];
  daily: { day: string; events: number; cost_micros: number }[];
  note: string;
}

export interface EventItem {
  partition: string;
  seq: number;
  event_id: string;
  event_type: string;
  recorded_at: string;
  request_id: string | null;
  channel: string | null;
  coverage: string | null;
  principal_id: string | null;
  register_id: string | null;
  decision: string | null;
  reason_code: string | null;
  policy_version: string | null;
  model: string | null;
  tool: string | null;
  risk_level: string | null;
  risk_score: number | null;
  cost_micros: number | null;
  usage_source: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  details: Record<string, unknown> | null;
  entry_hash: string;
  anchor_id: string | null;
  // Plain-language description (backend captain/api/describe.py)
  headline: string;
  outcome: string;
  outcome_tone: string;
  category: string;
  who: string;
  why: string | null;
}

export interface KeyView {
  key_id: string;
  principal_id: string;
  register_id: string;
  project_id: string;
  application_id: string;
  environment: string;
  status: string;
  label: string;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  revoked_reason: string | null;
  last_used_at: string | null;
}

export interface RegisterEntry {
  id: string;
  name: string;
  risk_tier: string;
  autonomy: string;
  owner_id: string;
  cost_centre: string;
  purpose: string;
  allowed_models: string[];
  allowed_tools: string[];
  active: boolean;
  eu_ai_act_high_risk: boolean;
  version: number;
}

export interface Budget {
  scope: string;
  period: string;
  limit_micros: number;
  hard: boolean;
  bucket: string;
  effective_limit_micros: number;
  spent_micros: number;
  reserved_micros: number;
  alert_thresholds: number[];
}

export interface KillEntry {
  scope: string;
  target: string;
  reason: string;
  actor_id: string;
  activated_at: string;
}

export interface Ticket {
  ticket_id: string;
  state: string;
  requested_by: string;
  register_id: string;
  action_summary: string;
  policy_version: string;
  assigned_approvers: string[];
  created_at: string;
  expires_at: string;
  escalated_at: string | null;
  approver_id: string | null;
  reason: string | null;
}

export interface Alert {
  id: string;
  kind: string;
  severity: string;
  subject: string;
  details: Record<string, unknown>;
  register_id: string | null;
  created_at: string;
  delivered: boolean;
  attempts: number;
  title: string;
  what_to_do: string;
}

export type CoverageClass = "GOVERNED" | "OBSERVED" | "BYPASS_DETECTED" | "UNOBSERVED";
export interface Coverage {
  period_days: number;
  total_events: number;
  classes: { coverage: CoverageClass; events: number; share_pct: number }[];
  by_channel: ({ channel: string } & Record<CoverageClass, number>)[];
  by_person: ({ principal_id: string } & Record<CoverageClass, number>)[];
  note: string;
}

export interface AgentRow {
  id: string;
  name: string;
  tier: string;
  autonomy: string;
  owner_id: string;
  active: boolean;
  allowed_models: string[];
  allowed_tools: string[];
  calls: number;
  refused: number;
  tools_held: number;
  tools_run: number;
  last_active: string | null;
  eval_scored: number;
  eval_mean: number | null;
  eval_low: number;
  eval_failed: number;
}

export interface PolicyInfo {
  bundle: { name?: string; version?: string } | null;
  tiers: Record<string, Record<string, unknown>> | null;
  tools: Record<string, unknown> | null;
}
export interface SimulationResult {
  decision: string;
  reason_code: string;
  explanation: string;
  policy_ids: string[];
  policy_bundle: string;
  policy_version: string;
  limits: Record<string, unknown>;
  simulated: boolean;
}

export interface RateLimit {
  scope: string;
  requests_per_minute: number | null;
  tokens_per_minute: number | null;
}

// ------------------------------------------------- company, users, setup
export interface UserView {
  id: string;
  email: string;
  name: string;
  team: string | null;
  job_title?: string | null;
  roles: string[];
  active: boolean;
  keys_revoked?: number;
}

export interface Contact {
  role: string;
  name: string;
  email: string;
}

export interface Organization {
  org_id?: string;
  company_name: string;
  legal_name: string;
  domains: string[];
  country: string;
  industry: string;
  contacts: Contact[];
  adopters: string[];
  checklist: Record<string, boolean>;
  updated_at: string | null;
  updated_by: string | null;
  sso_configured: boolean;
  checklist_items: { key: string; label: string; done: boolean }[];
}

/** Turns a one-time org-admin invite into a console session (cookie + CSRF token). */
export async function redeemInvite(token: string): Promise<SessionInfo> {
  const s = await api.post<SessionInfo>("/auth/invite/redeem", { token });
  setCsrf(s.csrf_token);
  return s;
}
