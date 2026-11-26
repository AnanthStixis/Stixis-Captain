// Money is stored in micro-units (1 USD = 1,000,000) and shown with enough precision
// that small development amounts are not rounded to zero.
export function money(micros: number | null | undefined): string {
  if (micros == null) return "—";
  const usd = micros / 1_000_000;
  if (usd !== 0 && Math.abs(usd) < 0.01) return `$${usd.toFixed(6)}`;
  return usd.toLocaleString(undefined, { style: "currency", currency: "USD" });
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

const SOURCE_NAMES: Record<string, string> = {
  anthropic_claude_code: "Claude Code (Anthropic analytics)",
  github_copilot: "GitHub Copilot",
  claude_code_hook: "Claude Code hook",
};

// Friendly name for an observed-usage source; CSV imports are labelled "csv:<vendor>".
export function sourceLabel(source: string): string {
  if (SOURCE_NAMES[source]) return SOURCE_NAMES[source];
  if (source.startsWith("csv:")) return `${source.slice(4)} (CSV export)`;
  return source;
}

export function pct(part: number, whole: number): number {
  return whole > 0 ? Math.min(100, Math.round((part / whole) * 100)) : 0;
}

export const DECISION_COLOR: Record<string, string> = {
  ALLOW: "teal",
  ALLOW_WITH_LIMITS: "teal",
  ALLOW_WITH_REDACTION: "blue",
  REQUIRE_APPROVAL: "yellow",
  BLOCK: "red",
  OBSERVE_ONLY: "gray",
};

export const RISK_COLOR: Record<string, string> = {
  LOW: "teal",
  MEDIUM: "yellow",
  HIGH: "orange",
  CRITICAL: "red",
};
