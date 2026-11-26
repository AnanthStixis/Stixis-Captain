import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, getToken, setCsrf, setToken } from "./api";
import { money, pct, sourceLabel } from "./format";

describe("format", () => {
  it("shows micro-unit money without rounding small dev amounts to zero", () => {
    expect(money(2_796)).toBe("$0.002796");
    expect(money(null)).toBe("—");
    expect(money(1_500_000)).toMatch(/1\.50/);
  });
  it("names observed-usage sources, including CSV exports", () => {
    expect(sourceLabel("github_copilot")).toBe("GitHub Copilot");
    expect(sourceLabel("csv:cursor")).toBe("cursor (CSV export)");
    expect(sourceLabel("something_new")).toBe("something_new");
  });
  it("caps percentages and handles zero limits", () => {
    expect(pct(50, 100)).toBe(50);
    expect(pct(150, 100)).toBe(100);
    expect(pct(1, 0)).toBe(0);
  });
});

describe("api client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setToken(null);
    setCsrf(null);
  });

  it("sends the bearer token and surfaces server error codes", async () => {
    setToken("t".repeat(40));
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: "FORBIDDEN", message: "requires keys:write" }), { status: 403 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.post("/keys", {})).rejects.toMatchObject({ status: 403, code: "FORBIDDEN" });
    const init = fetchMock.mock.calls[0]![1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe(`Bearer ${"t".repeat(40)}`);
  });

  it("clears the session on 401", async () => {
    setToken("t".repeat(40));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: "UNAUTHENTICATED" }), { status: 401 })));
    await expect(api.get("/me")).rejects.toBeInstanceOf(ApiError);
    expect(getToken()).toBeNull();
  });

  it("session cookies: sends the CSRF token on changes only, never an Authorization header", async () => {
    setCsrf("csrf-123");
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response("{}", { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    await api.get("/overview");
    await api.post("/killswitch", { scope: "model" });
    const [getInit, postInit] = fetchMock.mock.calls.map((c) => c[1] as RequestInit);
    expect(getInit!.credentials).toBe("same-origin");
    expect((getInit!.headers as Record<string, string>)["X-CSRF-Token"]).toBeUndefined();
    expect((postInit!.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-123");
    expect((postInit!.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("a 403 (permission or CSRF) does not end the session", async () => {
    setCsrf("csrf-123");
    setToken("t".repeat(40));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: "CSRF", message: "bad token" }), { status: 403 })));
    await expect(api.post("/keys", {})).rejects.toMatchObject({ status: 403, code: "CSRF" });
    expect(getToken()).not.toBeNull();
  });
});
