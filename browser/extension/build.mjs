// Build the extension for ONE Captain deployment:
//   node build.mjs --captain https://captain.example.com [--sites chatgpt.com,claude.ai] [--out dist]
// Writes dist/ (load it unpacked, or pack it). The manifest only lists the sites you watch and
// the one Captain address it may talk to: nothing broader.
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const arg = (name, def) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : def;
};

export function manifestFor(captain, sites, version = "0.1.0") {
  const u = new URL(captain);
  const local = ["127.0.0.1", "localhost", "[::1]", "::1"].includes(u.hostname) && u.protocol === "http:";
  if (u.protocol !== "https:" && !local) throw new Error("--captain must be an https:// address");
  if (!sites.length) throw new Error("at least one site is required");
  for (const s of sites) if (!/^[a-z0-9.-]+$/.test(s)) throw new Error(`bad site name: ${s}`);
  return {
    manifest_version: 3,
    name: "Stixis Captain",
    version,
    description: "Checks messages with Captain before they are sent to AI websites.",
    permissions: ["storage"],
    host_permissions: [`${u.origin}/*`],
    background: { service_worker: "background.js", type: "module" },
    content_scripts: [
      {
        matches: sites.flatMap((s) => [`https://${s}/*`, `https://*.${s}/*`]),
        js: ["content.js"],
        run_at: "document_start",
        all_frames: false,
      },
    ],
    options_ui: { page: "options.html", open_in_tab: true },
    storage: { managed_schema: "managed_schema.json" },
  };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const captain = arg("captain");
  if (!captain) {
    console.error("usage: node build.mjs --captain https://captain.example.com [--sites a.com,b.com] [--out dist]");
    process.exit(2);
  }
  const configured = Object.keys(JSON.parse(readFileSync(join(here, "sites.json"), "utf8")).sites);
  const sites = (arg("sites") ? arg("sites").split(",") : configured).map((s) => s.trim().toLowerCase());
  const out = join(here, arg("out", "dist"));
  rmSync(out, { recursive: true, force: true });
  mkdirSync(out, { recursive: true });
  cpSync(join(here, "src"), out, { recursive: true });
  cpSync(join(here, "managed_schema.json"), join(out, "managed_schema.json"));
  writeFileSync(join(out, "manifest.json"), JSON.stringify(manifestFor(captain, sites), null, 2));
  console.log(`built ${out} for ${new URL(captain).origin} watching: ${sites.join(", ")}`);
}
