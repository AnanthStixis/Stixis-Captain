import * as vscode from "vscode";

import { describe, fetchContinueConfig, fetchStatus, type FetchLike, type StatusResult } from "./core";

const KEY = "captain.key";
const REFRESH_MS = 60_000;

const http: FetchLike = (url, init) => fetch(url, init);

export function activate(context: vscode.ExtensionContext): void {
  const bar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  bar.command = "captain.showStatus";
  bar.show();
  let last: StatusResult = { kind: "no-key" };

  const gateway = () => vscode.workspace.getConfiguration("captain").get<string>("gatewayUrl", "http://127.0.0.1:8000");
  // Test networks only: lets the key travel over plain http (off by default).
  const allowHttp = () => vscode.workspace.getConfiguration("captain").get<boolean>("allowPlainHttp", false);

  async function refresh(): Promise<void> {
    last = await fetchStatus(http, gateway(), await context.secrets.get(KEY), 5000, allowHttp());
    const d = describe(last);
    bar.text = `$(shield) ${d.text}`;
    bar.tooltip = d.tooltip;
    bar.backgroundColor =
      d.level === "ok" ? undefined : new vscode.ThemeColor(d.level === "warn" ? "statusBarItem.warningBackground" : "statusBarItem.errorBackground");
  }

  context.subscriptions.push(
    bar,
    vscode.commands.registerCommand("captain.setKey", async () => {
      const value = await vscode.window.showInputBox({
        title: "Captain key",
        prompt: "Paste the Captain key issued to you. It is kept in this editor's secret storage.",
        password: true,
        ignoreFocusOut: true,
      });
      if (!value?.trim()) return;
      await context.secrets.store(KEY, value.trim());
      await refresh();
    }),
    vscode.commands.registerCommand("captain.clearKey", async () => {
      await context.secrets.delete(KEY);
      await refresh();
    }),
    vscode.commands.registerCommand("captain.refresh", refresh),
    vscode.commands.registerCommand("captain.showStatus", async () => {
      await refresh();
      const d = describe(last);
      void vscode.window.showInformationMessage(`${d.text}\n${d.tooltip}`, { modal: false });
    }),
    vscode.commands.registerCommand("captain.openContinueConfig", async () => {
      const key = await context.secrets.get(KEY);
      if (!key) {
        void vscode.window.showWarningMessage("Run “Captain: Set Key” first.");
        return;
      }
      const r = await fetchContinueConfig(http, gateway(), key, 5000, allowHttp());
      if (!r.ok) {
        void vscode.window.showErrorMessage(`Captain: ${r.reason}`);
        return;
      }
      // Shown for review, never written over the developer's own Continue settings.
      const doc = await vscode.workspace.openTextDocument({ language: "yaml", content: r.yaml });
      await vscode.window.showTextDocument(doc);
      void vscode.window.showInformationMessage(
        "Review this Continue configuration, replace PASTE_YOUR_CAPTAIN_KEY_HERE with your key, then merge it into your Continue config.",
      );
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("captain")) void refresh();
    }),
  );

  const timer = setInterval(() => void refresh(), REFRESH_MS);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
  void refresh();
}

export function deactivate(): void {}
