# Captain browser extension (Chrome / Edge, Manifest V3)

Checks a message with Captain **before it is submitted** to a watched AI website (ChatGPT, Claude, Gemini by default). Captain answers ALLOW, ALLOW with the personal data masked, or BLOCK, and the decision is recorded on the `BROWSER` channel (never the message text).

## What it can and cannot do (read this first)

- It governs messages submitted **in a browser profile where it is installed and active**, on the sites you list. It does **not** see another browser, an unmanaged profile, a direct API call, a desktop app, a mobile app, file uploads or anything typed outside a text box. Treat it as a control at one supported point, not as full coverage. The Coverage page counts it as governed only for what it actually checked.
- **Not verified against any live AI website.** Sites change their pages often. The detector is generic (Enter in an editable box, or a form submit) and releases an approved message by re-sending Enter. That was verified on a test page (allowed, masked and blocked cases) but **not** on ChatGPT, Claude or Gemini. Expect to adjust per site before relying on it. `sites.json` lists every site as `verified: false`.
- It cannot stop a determined user (disable the extension, use another browser). Pair it with managed-browser policy and network controls.

## Build for one deployment

```powershell
node build.mjs --captain https://captain.example.com            # all sites in sites.json
node build.mjs --captain https://captain.example.com --sites claude.ai,chatgpt.com
```

The manifest only lists the sites you watch and the one Captain address it may contact. `dist/` is the extension; load it unpacked to try it (`chrome://extensions`, Developer mode).

## Managed deployment (IT)

1. Pack and host the extension, then force-install it with the browser policy `ExtensionInstallForcelist` (Chrome Enterprise / Edge policy).
2. Configure it per user or device with the policy `3rdparty.extensions.<id>` (managed storage, schema in `managed_schema.json`): `captainUrl`, `captainKey`, `failMode` (`closed` default: if Captain cannot be reached the message is **not** sent; `open`: it is sent).
3. **The key in managed policy is readable by anyone who can read the policy and by the user's profile.** Issue each employee a separate virtual key with a small budget (Captain can revoke or rotate it), never a shared admin key.
4. Without managed settings the employee can paste the address and key on the extension's options page (stored in the browser profile).

## Behaviour

| Captain says | The user sees |
|---|---|
| ALLOW | Message is sent normally |
| ALLOW_WITH_REDACTION | Personal data is replaced in the box, a blue note appears, then it is sent |
| BLOCK (secret, kill switch, policy) | Red note with the reason; the text stays in the box, nothing is sent |
| Captain unreachable | `failMode`: closed = blocked with a note; open = sent |
| Unreadable answer, bad key, text over 100,000 characters | Blocked with a note |

## Tests

`npm test` covers the logic (site matching, answer handling, request building, manifest limits). The page behaviour (intercept, mask, block) was checked by hand on a local test page against the live gateway; there is no automated browser test yet.
