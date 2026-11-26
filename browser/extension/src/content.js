// Content script, injected only on the watched AI sites. Thin DOM glue: it catches the
// moment a message is submitted (Enter in a textbox, or a form submit), asks the background
// worker whether Captain allows it, then releases, masks or blocks it. It never holds the
// Captain key and never stores message text.
//
// NOT VERIFIED against any live AI website. Sites change their pages; the generic detector
// below (Enter in an editable box / form submit) may need a per-site adjustment, and
// releasing the held message by re-dispatching Enter may not work on every site.
(() => {
  const SITE = location.hostname.toLowerCase();
  let releasing = false;
  let busy = false;

  function editableOf(node) {
    if (!(node instanceof Element)) return null;
    return node.closest('textarea, input[type="text"], input:not([type]), [contenteditable=""], [contenteditable="true"], [role="textbox"]');
  }
  const textOf = (el) => ("value" in el ? el.value : el.innerText || "");

  function setText(el, text) {
    if ("value" in el) {
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text); // framework-safe setter
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      el.focus();
      document.execCommand("selectAll", false);
      document.execCommand("insertText", false, text);
    }
  }

  function banner(message, kind) {
    if (!message) return;
    const old = document.getElementById("captain-banner");
    if (old) old.remove();
    const b = document.createElement("div");
    b.id = "captain-banner";
    b.setAttribute("role", "alert");
    b.textContent = `Captain: ${message}`; // textContent only: never markup from a response
    Object.assign(b.style, {
      position: "fixed", top: "12px", right: "12px", zIndex: "2147483647", maxWidth: "360px",
      padding: "10px 14px", borderRadius: "8px", font: "14px system-ui, sans-serif",
      color: "#fff", background: kind === "block" ? "#b42318" : "#1d4ed8",
      boxShadow: "0 4px 16px rgba(0,0,0,.25)",
    });
    document.documentElement.appendChild(b);
    setTimeout(() => b.remove(), 9000);
  }

  function release(el) {
    releasing = true;
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
    setTimeout(() => { releasing = false; }, 0);
  }

  async function guard(el, text) {
    if (busy) return;
    busy = true;
    try {
      const answer = await chrome.runtime.sendMessage({ type: "check", site: SITE, text });
      if (!answer || answer.action === "block") {
        banner((answer && answer.message) || "Blocked.", "block");
        return;
      }
      if (answer.action === "send_masked") {
        setText(el, answer.text);
        banner(answer.message, "info");
      }
      release(el);
    } catch {
      banner("Captain could not be reached, so this message was not sent.", "block");
    } finally {
      busy = false;
    }
  }

  document.addEventListener(
    "keydown",
    (e) => {
      if (releasing || e.key !== "Enter" || e.shiftKey || e.isComposing) return;
      if (!e.isTrusted) return; // only a real keystroke starts a check
      const el = editableOf(e.target);
      if (!el) return;
      const text = textOf(el).trim();
      if (!text) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      guard(el, text);
    },
    true,
  );

  document.addEventListener(
    "submit",
    (e) => {
      if (releasing || !(e.target instanceof HTMLFormElement)) return;
      const el = editableOf(e.target.querySelector("textarea, [contenteditable], input[type=text]"));
      const text = el ? textOf(el).trim() : "";
      if (!el || !text) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      guard(el, text);
    },
    true,
  );
})();
