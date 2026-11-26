/** Errors the Captain gateway can return. Every refusal carries Captain's reason `code`
 * (for example BUDGET_EXHAUSTED or KILL_SWITCH_ACTIVE), so callers can react to the reason. */

export class CaptainError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly code?: string,
    readonly requestId?: string,
  ) {
    super(
      [message, code ? `[${code}]` : "", requestId ? `(request ${requestId})` : ""]
        .filter(Boolean)
        .join(" "),
    );
    this.name = new.target.name;
  }
}

/** The key is missing, wrong, revoked or expired (HTTP 401). */
export class AuthenticationError extends CaptainError {}
/** Captain's policy refused the call (HTTP 403). */
export class RefusedError extends CaptainError {}
/** A budget or rate limit stopped the call (HTTP 429). It cost nothing. */
export class BudgetExceededError extends CaptainError {}
/** The call needs a human approval, which a model call cannot wait for. */
export class ApprovalRequiredError extends RefusedError {}
/** The model's answer was stopped by output guardrails. */
export class OutputBlockedError extends RefusedError {}
/** The idempotency key was already used (HTTP 409); the original call was not repeated. */
export class DuplicateRequestError extends CaptainError {}
/** Every model route failed (HTTP 502). */
export class ProviderError extends CaptainError {}
/** Captain could not decide safely (HTTP 503), or could not be reached. */
export class UnavailableError extends CaptainError {}

export function fromResponse(status: number, body: unknown, requestId?: string): CaptainError {
  const err =
    body && typeof body === "object" && "error" in body && typeof (body as { error: unknown }).error === "object"
      ? ((body as { error: Record<string, unknown> }).error ?? {})
      : {};
  const code = err["code"] ? String(err["code"]) : undefined;
  const message = String(err["message"] ?? `gateway answered HTTP ${status}`);
  const args = [message, status, code, requestId] as const;
  if (status === 401) return new AuthenticationError(...args);
  if (status === 409 && code === "IDEMPOTENCY_REPLAY") return new DuplicateRequestError(...args);
  if (status === 429) return new BudgetExceededError(...args);
  if (status === 502) return new ProviderError(...args);
  if (status >= 500) return new UnavailableError(...args);
  if (code === "OUTPUT_BLOCKED") return new OutputBlockedError(...args);
  if (code === "APPROVAL_REQUIRED" || message.includes("requires human approval"))
    return new ApprovalRequiredError(...args);
  if (status === 403) return new RefusedError(...args);
  return new CaptainError(...args);
}
