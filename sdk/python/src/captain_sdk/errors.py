"""Errors the Captain gateway can return, as Python exceptions.

Every refusal carries Captain's `reason_code` (for example BUDGET_EXHAUSTED or
KILL_SWITCH_ACTIVE), so callers can react to the reason instead of parsing text.
"""

from __future__ import annotations


class CaptainError(Exception):
    """Base class. `status` is the HTTP status, `code` the Captain reason code."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message, self.status, self.code, self.request_id = message, status, code, request_id

    def __str__(self) -> str:
        bits = [self.message]
        if self.code:
            bits.append(f"[{self.code}]")
        if self.request_id:
            bits.append(f"(request {self.request_id})")
        return " ".join(bits)


class AuthenticationError(CaptainError):
    """The key is missing, wrong, revoked, or expired (HTTP 401)."""


class RefusedError(CaptainError):
    """Captain's policy refused the call (HTTP 403): kill switch, model or tool not allowed,
    content blocked, no route, approval needed, ..."""


class BudgetExceededError(CaptainError):
    """A budget or rate limit stopped the call (HTTP 429). It cost nothing."""


class ApprovalRequiredError(RefusedError):
    """The call needs a human approval, which a model call cannot wait for."""


class OutputBlockedError(RefusedError):
    """The model's answer was stopped by output guardrails (a secret in the reply, ...)."""


class DuplicateRequestError(CaptainError):
    """The idempotency key was already used (HTTP 409); the original call was not repeated."""


class ProviderError(CaptainError):
    """Every model route failed (HTTP 502)."""


class UnavailableError(CaptainError):
    """Captain could not decide safely (HTTP 503), or could not be reached."""


def from_response(status: int, body: object, request_id: str | None = None) -> CaptainError:
    """Turn a gateway error body (OpenAI shape: {"error": {"code", "message"}}) into an
    exception of the right class."""
    err = body.get("error") if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else {}
    code = str(err["code"]) if err.get("code") else None
    message = str(err.get("message") or f"gateway answered HTTP {status}")
    args = (message, status, code, request_id)
    if status == 401:
        return AuthenticationError(*args)
    if status == 409 and code == "IDEMPOTENCY_REPLAY":
        return DuplicateRequestError(*args)
    if status == 429:
        return BudgetExceededError(*args)
    if status == 502:
        return ProviderError(*args)
    if status >= 500:
        return UnavailableError(*args)
    if code == "OUTPUT_BLOCKED":
        return OutputBlockedError(*args)
    if code == "APPROVAL_REQUIRED" or "requires human approval" in message:
        return ApprovalRequiredError(*args)
    if status == 403:
        return RefusedError(*args)
    return CaptainError(*args)
