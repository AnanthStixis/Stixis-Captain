using System.Text.Json;

namespace Stixis.Captain.Sdk;

/// <summary>Base class. <see cref="Status"/> is the HTTP status, <see cref="Code"/> Captain's reason code.</summary>
public class CaptainException : Exception
{
    public int? Status { get; }
    public string? Code { get; }
    public string? RequestId { get; }

    public CaptainException(string message, int? status = null, string? code = null, string? requestId = null)
        : base(Compose(message, code, requestId))
    {
        Status = status;
        Code = code;
        RequestId = requestId;
    }

    private static string Compose(string message, string? code, string? requestId) =>
        string.Join(" ", new[] { message, code is null ? null : $"[{code}]", requestId is null ? null : $"(request {requestId})" }
            .Where(s => !string.IsNullOrEmpty(s)));

    internal static CaptainException From(int status, string? body, string? requestId)
    {
        string? code = null;
        var message = $"gateway answered HTTP {status}";
        try
        {
            if (!string.IsNullOrEmpty(body))
            {
                using var doc = JsonDocument.Parse(body);
                if (doc.RootElement.ValueKind == JsonValueKind.Object &&
                    doc.RootElement.TryGetProperty("error", out var err) && err.ValueKind == JsonValueKind.Object)
                {
                    if (err.TryGetProperty("code", out var c) && c.ValueKind == JsonValueKind.String) code = c.GetString();
                    if (err.TryGetProperty("message", out var m) && m.ValueKind == JsonValueKind.String) message = m.GetString() ?? message;
                }
            }
        }
        catch (JsonException) { /* a non-JSON body still maps by status */ }

        if (status == 401) return new AuthenticationException(message, status, code, requestId);
        if (status == 409 && code == "IDEMPOTENCY_REPLAY") return new DuplicateRequestException(message, status, code, requestId);
        if (status == 429) return new BudgetExceededException(message, status, code, requestId);
        if (status == 502) return new ProviderException(message, status, code, requestId);
        if (status >= 500) return new UnavailableException(message, status, code, requestId);
        if (code == "OUTPUT_BLOCKED") return new OutputBlockedException(message, status, code, requestId);
        if (code == "APPROVAL_REQUIRED" || message.Contains("requires human approval", StringComparison.Ordinal))
            return new ApprovalRequiredException(message, status, code, requestId);
        if (status == 403) return new RefusedException(message, status, code, requestId);
        return new CaptainException(message, status, code, requestId);
    }
}

/// <summary>The key is missing, wrong, revoked or expired (HTTP 401).</summary>
public class AuthenticationException(string m, int? s = null, string? c = null, string? r = null) : CaptainException(m, s, c, r);
/// <summary>Captain's policy refused the call (HTTP 403).</summary>
public class RefusedException(string m, int? s = null, string? c = null, string? r = null) : CaptainException(m, s, c, r);
/// <summary>A budget or rate limit stopped the call (HTTP 429). It cost nothing.</summary>
public class BudgetExceededException(string m, int? s = null, string? c = null, string? r = null) : CaptainException(m, s, c, r);
/// <summary>The call needs a human approval, which a model call cannot wait for.</summary>
public class ApprovalRequiredException(string m, int? s = null, string? c = null, string? r = null) : RefusedException(m, s, c, r);
/// <summary>The model's answer was stopped by output guardrails.</summary>
public class OutputBlockedException(string m, int? s = null, string? c = null, string? r = null) : RefusedException(m, s, c, r);
/// <summary>The idempotency key was already used (HTTP 409); the original call was not repeated.</summary>
public class DuplicateRequestException(string m, int? s = null, string? c = null, string? r = null) : CaptainException(m, s, c, r);
/// <summary>Every model route failed (HTTP 502).</summary>
public class ProviderException(string m, int? s = null, string? c = null, string? r = null) : CaptainException(m, s, c, r);
/// <summary>Captain could not decide safely (HTTP 503), or could not be reached.</summary>
public class UnavailableException(string m, int? s = null, string? c = null, string? r = null) : CaptainException(m, s, c, r);
