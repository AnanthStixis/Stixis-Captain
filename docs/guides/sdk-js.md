# JavaScript / TypeScript SDK

**What Captain governs:** model calls from your own Node.js (20+) applications.

## Before you start
- A **Captain key** for the application, kept in your secret store.
- The package from your platform team (`sdk/js` in the Captain release, `@stixis/captain-sdk`).

## Use
```ts
import { CaptainClient } from "@stixis/captain-sdk";

const captain = new CaptainClient({ apiKey: process.env.CAPTAIN_KEY!, baseUrl: "{{GATEWAY_URL}}" });
const r = await captain.chat("claude-sonnet", [{ role: "user", content: "Hello" }]);
console.log(r.text, r.decision);
```

## Test it
Run it. **Expected:** an answer; **Activity** shows the request under your application.
