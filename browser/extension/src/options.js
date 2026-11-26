const $ = (id) => document.getElementById(id);

chrome.storage.local.get(["captainUrl", "captainKey", "failMode"]).then((v) => {
  $("url").value = v.captainUrl || "";
  if (!v.captainUrl) {
    // Pre-fill the address from a package downloaded from the Captain dashboard.
    fetch(chrome.runtime.getURL("preset.json"))
      .then((r) => (r.ok ? r.json() : {}))
      .then((p) => {
        if (p.captainUrl && !$("url").value) $("url").value = p.captainUrl;
      })
      .catch(() => {});
  }
  $("key").value = v.captainKey || "";
  $("fail").value = v.failMode || "closed";
});

$("save").addEventListener("click", async () => {
  const url = $("url").value.trim().replace(/\/+$/, "");
  const local = /^http:\/\/(127\.0\.0\.1|localhost|\[::1\])(:|\/|$)/.test(url);
  if (!url.startsWith("https://") && !local) {
    $("status").textContent = "The address must start with https://";
    return;
  }
  await chrome.storage.local.set({ captainUrl: url, captainKey: $("key").value.trim(), failMode: $("fail").value });
  $("status").textContent = "Saved.";
});
