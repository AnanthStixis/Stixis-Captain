// Wiring test with a fake `vscode` module: commands are registered, the key lives in
// secret storage, and the status bar follows the gateway. Real VS Code is not started.
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const registered = new Map<string, (...a: unknown[]) => unknown>();
const bar: { text: string; tooltip: string; backgroundColor: unknown; command?: string; show: () => void; dispose: () => void } = {
  text: "",
  tooltip: "",
  backgroundColor: undefined,
  show: () => {},
  dispose: () => {},
};
const store = new Map<string, string>();
const shown: string[] = [];
let inputValue: string | undefined;
let opened: { language: string; content: string } | undefined;

vi.mock("vscode", () => ({
  StatusBarAlignment: { Left: 1 },
  ThemeColor: class {
    constructor(public id: string) {}
  },
  window: {
    createStatusBarItem: () => bar,
    showInputBox: async () => inputValue,
    showInformationMessage: async (m: string) => void shown.push(m),
    showWarningMessage: async (m: string) => void shown.push(m),
    showErrorMessage: async (m: string) => void shown.push(m),
    showTextDocument: async () => {},
  },
  commands: {
    registerCommand: (id: string, fn: (...a: unknown[]) => unknown) => {
      registered.set(id, fn);
      return { dispose: () => {} };
    },
  },
  workspace: {
    getConfiguration: () => ({ get: (_: string, d: string) => d }),
    onDidChangeConfiguration: () => ({ dispose: () => {} }),
    openTextDocument: async (o: { language: string; content: string }) => {
      opened = o;
      return {};
    },
  },
}));

const status = {
  principal: "dev-app",
  register_id: "AI-DEV",
  system: "Developer sandbox",
  risk_tier: "TIER_3",
  autonomy: "A2",
  project: "p",
  application: "a",
  environment: "DEVELOPMENT",
  allowed_models: ["claude-sonnet"],
};

const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  registered.clear();
  store.clear();
  shown.length = 0;
  opened = undefined;
  vi.stubGlobal("fetch", async (url: string, init: { headers: Record<string, string> }) => {
    const ok = init.headers.Authorization === "Bearer cpt_good";
    if (!ok) return { ok: false, status: 401, json: async () => ({}), text: async () => "" };
    const body = url.endsWith("/v1/ide/continue") ? "name: Captain" : status;
    return { ok: true, status: 200, json: async () => body, text: async () => String(body) };
  });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function activate() {
  const { activate: act } = await import("../src/extension");
  const subs: { dispose(): void }[] = [];
  const ctx = {
    subscriptions: subs,
    secrets: {
      get: async (k: string) => store.get(k),
      store: async (k: string, v: string) => void store.set(k, v),
      delete: async (k: string) => void store.delete(k),
    },
  };
  act(ctx as never);
  await flush();
  return subs;
}

it("registers every command and starts out not signed in", async () => {
  await activate();
  expect([...registered.keys()].sort()).toEqual([
    "captain.clearKey",
    "captain.openContinueConfig",
    "captain.refresh",
    "captain.setKey",
    "captain.showStatus",
  ]);
  expect(bar.text).toContain("not signed in");
});

it("setting a key stores it as a secret and shows the system and tier", async () => {
  await activate();
  inputValue = "  cpt_good ";
  await registered.get("captain.setKey")?.();
  expect(store.get("captain.key")).toBe("cpt_good");
  expect(bar.text).toContain("AI-DEV · Tier 3");
  expect(bar.backgroundColor).toBeUndefined();
});

it("a rejected key and a cleared key change the bar", async () => {
  await activate();
  inputValue = "cpt_bad";
  await registered.get("captain.setKey")?.();
  expect(bar.text).toContain("key rejected");
  await registered.get("captain.clearKey")?.();
  expect(store.has("captain.key")).toBe(false);
  expect(bar.text).toContain("not signed in");
});

it("opens the Continue config for review without writing any file", async () => {
  await activate();
  await registered.get("captain.openContinueConfig")?.();
  expect(shown.at(-1)).toContain("Set Key");
  inputValue = "cpt_good";
  await registered.get("captain.setKey")?.();
  await registered.get("captain.openContinueConfig")?.();
  expect(opened).toEqual({ language: "yaml", content: "name: Captain" });
  expect(shown.at(-1)).toContain("PASTE_YOUR_CAPTAIN_KEY_HERE");
});
