// Pure logic of the Captain browser extension: no browser APIs, so it is unit-tested in Node.

/** The host of a page URL, lower case, or null. */
export function hostOf(url) {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return null;
  }
}

/** Is this host one of the watched sites (exact match or a subdomain)? */
export function siteMatches(host, sites) {
  if (!host) return null;
  for (const s of Object.keys(sites)) {
    const k = s.toLowerCase();
    if (host === k || host.endsWith("." + k)) return k;
  }
  return null;
}

/**
 * Decide what to do with a Captain answer.
 * Returns { action: "send" | "send_masked" | "block", text?, message }.
 * Anything unexpected is a block: an answer we cannot read is never treated as "allow".
 */
export function interpret(resp, original) {
  if (!resp || typeof resp !== "object" || typeof resp.allowed !== "boolean") {
    return { action: "block", message: "Captain sent an answer the extension cannot read." };
  }
  if (!resp.allowed) {
    return { action: "block", message: resp.message || "Blocked by Captain policy.", reason: resp.reason_code };
  }
  if (resp.masked_text && resp.masked_text !== original) {
    return { action: "send_masked", text: resp.masked_text, message: "Personal data was masked before sending." };
  }
  return { action: "send", message: "" };
}

/** What to do when Captain cannot be reached: depends on the deployment's fail mode. */
export function onUnreachable(failMode) {
  return failMode === "open"
    ? { action: "send", message: "", warned: true }
    : { action: "block", message: "Captain could not be reached, so this message was not sent. Try again, or contact IT." };
}

/** The request the background worker sends to the gateway. */
export function buildRequest(captainUrl, key, site, text) {
  const base = String(captainUrl || "").replace(/\/+$/, "");
  const local = /^http:\/\/(127\.0\.0\.1|localhost|\[::1\])(:|\/|$)/.test(base);
  if (!base.startsWith("https://") && !local) {
    throw new Error("Captain URL must use https (plain http is allowed only for this machine)");
  }
  if (!key) throw new Error("no Captain key configured");
  return {
    url: `${base}/v1/browser/check`,
    init: {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify({ site, text }),
      redirect: "manual",
    },
  };
}

/** Texts longer than the gateway accepts are blocked here with a clear reason. */
export const MAX_TEXT = 100000;
export function tooLong(text) {
  return text.length > MAX_TEXT;
}
