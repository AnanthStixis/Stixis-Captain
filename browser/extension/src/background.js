// Service worker: holds the Captain key and talks to the gateway. The content script never
// sees the key. Messages: { type: "check", site, text } -> { action, text?, message }.
import { buildRequest, interpret, onUnreachable, tooLong } from "./core.js";

async function config() {
  // Managed (IT-deployed) settings win over what the user typed on the options page.
  // A package downloaded from the Captain dashboard carries preset.json with that install's
  // address (never a key), used when neither IT nor the user has set one.
  const [managed, local, preset] = await Promise.all([
    chrome.storage.managed.get(["captainUrl", "captainKey", "failMode"]).catch(() => ({})),
    chrome.storage.local.get(["captainUrl", "captainKey", "failMode"]),
    fetch(chrome.runtime.getURL("preset.json"))
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({})),
  ]);
  return {
    captainUrl: managed.captainUrl || local.captainUrl || preset.captainUrl,
    captainKey: managed.captainKey || local.captainKey,
    failMode: managed.failMode || local.failMode || "closed",
    preset,
  };
}

async function check(site, text) {
  const cfg = await config();
  if (tooLong(text)) return { action: "block", message: "This message is too long to check." };
  let req;
  try {
    req = buildRequest(cfg.captainUrl, cfg.captainKey, site, text, cfg.preset);
  } catch (e) {
    return { action: "block", message: `Captain is not set up on this browser: ${e.message}` };
  }
  let res;
  try {
    res = await fetch(req.url, req.init);
  } catch {
    return onUnreachable(cfg.failMode);
  }
  if (res.status === 401) return { action: "block", message: "The Captain key was rejected. Ask IT for a new one." };
  if (res.status >= 500) return onUnreachable(cfg.failMode);
  if (!res.ok) return { action: "block", message: `Captain answered HTTP ${res.status}.` };
  let body;
  try {
    body = await res.json();
  } catch {
    return { action: "block", message: "Captain sent an answer the extension cannot read." };
  }
  return interpret(body, text);
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "check" && typeof msg.text === "string" && typeof msg.site === "string") {
    check(msg.site, msg.text).then(sendResponse, () =>
      sendResponse({ action: "block", message: "The check failed unexpectedly." }),
    );
    return true; // async response
  }
  return false;
});
