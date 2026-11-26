import assert from "node:assert/strict";
import { test } from "node:test";

import { manifestFor } from "../build.mjs";
import { MAX_TEXT, buildRequest, hostOf, interpret, onUnreachable, siteMatches, tooLong } from "../src/core.js";

const SITES = { "chatgpt.com": {}, "claude.ai": {} };

test("sites match by host or subdomain, never by substring", () => {
  assert.equal(siteMatches("chatgpt.com", SITES), "chatgpt.com");
  assert.equal(siteMatches("chat.claude.ai", SITES), "claude.ai");
  assert.equal(siteMatches("evilchatgpt.com", SITES), null);
  assert.equal(siteMatches("chatgpt.com.evil.example", SITES), null);
  assert.equal(siteMatches(null, SITES), null);
  assert.equal(hostOf("https://Claude.AI/chat/1"), "claude.ai");
  assert.equal(hostOf("not a url"), null);
});

test("an allowed answer sends, a masked answer sends the masked text, a block blocks", () => {
  assert.deepEqual(interpret({ allowed: true, masked_text: null }, "hi"), { action: "send", message: "" });
  const masked = interpret({ allowed: true, masked_text: "mail [EMAIL]" }, "mail a@b.com");
  assert.equal(masked.action, "send_masked");
  assert.equal(masked.text, "mail [EMAIL]");
  const blocked = interpret({ allowed: false, message: "A secret was detected", reason_code: "SECRET_DETECTED" }, "x");
  assert.equal(blocked.action, "block");
  assert.match(blocked.message, /secret/);
  assert.equal(blocked.reason, "SECRET_DETECTED");
});

test("an unreadable answer is a block, never an allow", () => {
  for (const bad of [null, undefined, "yes", {}, { allowed: "true" }, { decision: "ALLOW" }]) {
    assert.equal(interpret(bad, "x").action, "block");
  }
});

test("when Captain is unreachable the fail mode decides; closed is the default", () => {
  assert.equal(onUnreachable("closed").action, "block");
  assert.equal(onUnreachable(undefined).action, "block");
  assert.equal(onUnreachable("open").action, "send");
});

test("the request carries the key in the header and the text in the body, only over https", () => {
  const r = buildRequest("https://captain.example.com/", "k1", "chatgpt.com", "hello");
  assert.equal(r.url, "https://captain.example.com/v1/browser/check");
  assert.equal(r.init.headers.Authorization, "Bearer k1");
  assert.deepEqual(JSON.parse(r.init.body), { site: "chatgpt.com", text: "hello" });
  assert.equal(r.init.redirect, "manual");
  assert.throws(() => buildRequest("http://captain.example.com", "k", "a.com", "x"), /https/);
  assert.throws(() => buildRequest("https://captain.example.com", "", "a.com", "x"), /key/);
  assert.doesNotThrow(() => buildRequest("http://127.0.0.1:8000", "k", "a.com", "x"));
});

test("long text is refused before it is sent", () => {
  assert.equal(tooLong("x".repeat(MAX_TEXT)), false);
  assert.equal(tooLong("x".repeat(MAX_TEXT + 1)), true);
});

test("the manifest is limited to the watched sites and the one Captain address", () => {
  const m = manifestFor("https://captain.example.com", ["chatgpt.com", "claude.ai"]);
  assert.equal(m.manifest_version, 3);
  assert.deepEqual(m.host_permissions, ["https://captain.example.com/*"]);
  assert.deepEqual(m.content_scripts[0].matches, [
    "https://chatgpt.com/*", "https://*.chatgpt.com/*", "https://claude.ai/*", "https://*.claude.ai/*",
  ]);
  assert.deepEqual(m.permissions, ["storage"]);
  assert.throws(() => manifestFor("http://captain.example.com", ["a.com"]), /https/);
  assert.throws(() => manifestFor("https://captain.example.com", []), /site/);
  assert.throws(() => manifestFor("https://captain.example.com", ["a.com/../x"]), /bad site/);
});
