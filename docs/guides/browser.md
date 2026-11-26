# Browser extension (ChatGPT, Claude and Gemini websites)

**What Captain governs:** text typed into those AI websites, checked before it is sent. Personal data is replaced with placeholders; secrets and prompt-injection text are blocked; every check is recorded on the BROWSER channel (never the text).

**Honest limit:** it covers browsers where the extension is installed (Chrome or Edge). Another browser, profile or a desktop app is not covered: use your device management to require it.

## Before you start
- A **Captain key** per user (or per device), created on **Captain keys**.
- Chrome or Edge.

## Try it on one computer
1. Download **Browser extension** (`captain-browser.zip`) and unzip it to a folder.
2. Open `chrome://extensions` (or `edge://extensions`), switch on **Developer mode**, click **Load unpacked**, pick the folder.
3. Open the extension's **Options**. The address is already filled in (`{{GATEWAY_URL}}`). Paste your Captain key, **Save**.

## Deploy to everyone (Intune or Group Policy)
1. Publish the extension in your organisation's extension store (or host it) and force-install it with your device management.
2. Download **Intune / GPO policy** (`captain-browser-policy.json`). It sets `captainUrl` for everyone; set `captainKey` per user with your tool. Managed settings win over what a user types.

## Test it
1. On chatgpt.com type `My email is test.person@example.com` and send. **Expected:** the message is sent with the email replaced by a placeholder. **Activity** shows "Allowed, data hidden".
2. Type a fake secret such as `AKIAABCDEFGHIJKLMNOP` and send. **Expected:** blocked with a message. **Activity** shows "Blocked: A secret ... was found."

## If something goes wrong
| You see | Likely cause | Fix |
|---|---|---|
| Nothing happens on the site | Extension not active on that site | Check it is enabled; reload the page |
| "Captain is unreachable" | Wrong address or network | Check `{{GATEWAY_URL}}/health` opens |
