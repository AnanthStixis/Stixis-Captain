import { describe as suite, expect, it } from "vitest";

import { checkGatewayUrl, describe, fetchContinueConfig, fetchStatus, type FetchLike } from "../src/core";

const STATUS = {
  principal: "dev-app",
  register_id: "AI-DEV",
  system: "Developer sandbox",
  risk_tier: "TIER_3",
  autonomy: "A2",
  project: "dev-project",
  application: "dev-application",
  environment: "DEVELOPMENT",
  allowed_models: ["claude-sonnet"],
};

function fake(status: number, body: unknown, seen: { url?: string; auth?: string } = {}): FetchLike {
  return async (url, init) => {
    seen.url = url;
    seen.auth = init.headers.Authorization;
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
      text: async () => String(body),
    };
  };
}

suite("checkGatewayUrl", () => {
  it("allows https and loopback http only", () => {
    expect(checkGatewayUrl("https://captain.example.com/")).toEqual({ ok: true, base: "https://captain.example.com" });
    expect(checkGatewayUrl("http://127.0.0.1:8000")).toEqual({ ok: true, base: "http://127.0.0.1:8000" });
    expect(checkGatewayUrl("http://localhost:8000/")).toMatchObject({ ok: true });
    for (const bad of ["http://captain.example.com", "ftp://x", "not a url", "https://u:p@captain.example.com"]) {
      expect(checkGatewayUrl(bad).ok).toBe(false);
    }
  });
});

suite("fetchStatus", () => {
  it("sends the key as a bearer header, never in the URL", async () => {
    const seen: { url?: string; auth?: string } = {};
    const r = await fetchStatus(fake(200, STATUS, seen), "http://127.0.0.1:8000", "cpt_secret");
    expect(r.kind).toBe("ok");
    expect(seen.auth).toBe("Bearer cpt_secret");
    expect(seen.url).toBe("http://127.0.0.1:8000/v1/governance/status");
    expect(seen.url).not.toContain("cpt_secret");
  });

  it("reports each failure honestly", async () => {
    expect((await fetchStatus(fake(200, STATUS), "http://127.0.0.1:8000", undefined)).kind).toBe("no-key");
    expect((await fetchStatus(fake(401, {}), "http://127.0.0.1:8000", "k")).kind).toBe("rejected");
    expect(await fetchStatus(fake(503, {}), "http://127.0.0.1:8000", "k")).toEqual({ kind: "error", status: 503 });
    expect((await fetchStatus(fake(200, { nope: 1 }), "http://127.0.0.1:8000", "k")).kind).toBe("error");
    const down: FetchLike = async () => {
      throw new Error("ECONNREFUSED");
    };
    expect((await fetchStatus(down, "http://127.0.0.1:8000", "k")).kind).toBe("unreachable");
  });

  it("does not send the key to an unsafe URL", async () => {
    let called = false;
    const spy: FetchLike = async () => {
      called = true;
      throw new Error("must not be called");
    };
    const r = await fetchStatus(spy, "http://captain.example.com", "k");
    expect(r.kind).toBe("bad-url");
    expect(called).toBe(false);
  });
});

suite("describe", () => {
  it("shows the system and tier, with an honest scope note", () => {
    const d = describe({ kind: "ok", status: STATUS });
    expect(d.text).toBe("Captain: AI-DEV · Tier 3");
    expect(d.level).toBe("ok");
    expect(d.tooltip).toContain("claude-sonnet");
    expect(d.tooltip).toContain("does not watch other AI tools");
  });

  it("flags every non-ok state", () => {
    for (const r of [
      { kind: "no-key" },
      { kind: "rejected" },
      { kind: "unreachable" },
      { kind: "error", status: 500 },
      { kind: "bad-url", reason: "x" },
    ] as const) {
      expect(describe(r).level).not.toBe("ok");
    }
  });
});

suite("fetchContinueConfig", () => {
  it("returns the YAML and maps failures to reasons", async () => {
    expect(await fetchContinueConfig(fake(200, "name: Captain"), "http://127.0.0.1:8000", "k")).toEqual({
      ok: true,
      yaml: "name: Captain",
    });
    expect(await fetchContinueConfig(fake(401, ""), "http://127.0.0.1:8000", "k")).toMatchObject({ ok: false });
    expect(await fetchContinueConfig(fake(200, ""), "http://evil.example", "k")).toMatchObject({ ok: false });
  });
});
