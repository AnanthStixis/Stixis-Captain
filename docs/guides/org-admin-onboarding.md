# Org admin: from first sign-in to a working setup

You are the first administrator of your company's Captain. Stixis has sent you a one-time sign-in link. This guide takes you from that link to people using AI through Captain. Plan about an hour.

## 1. Sign in
1. Open the link from Stixis. It works once and expires after 7 days.
2. **Expected:** the Captain dashboard opens on **Setup**, with a checklist.

## 2. Connect your company sign-in (SSO)
Do this first: after it, everyone signs in with their normal work account and the one-time link stops working.
Everything happens on **Setup → Connect your sign-in**; nothing to install or edit on a server.
1. **Register Captain in your provider** (your identity team, once): a web app called "Captain", with the redirect address shown on the page (press **Copy**), a client secret, and a groups claim in its tokens (Entra: *Token configuration → Add groups claim*).
2. **Enter its details**: pick Microsoft Entra ID, Okta, Google or Other; enter the tenant ID / Okta domain / issuer, the application (client) ID and the client secret (stored encrypted, never shown again). Optionally map groups to Captain roles (roles given on **Users** also count). **Save**, then **Check connection**. **Expected:** "Captain can reach your provider".
3. **Test sign-in**: you go to your provider, sign in with **your own** account and come back. **Expected:** "Test sign-in worked". It only passes if that account would be an org admin under SSO, so you cannot lock your company out. Your current session stays open.
4. **Turn on SSO**. **Expected:** you are signed out; sign in with **Sign in with SSO**. One-time links and email sign-in links stop working.
- Changing the settings later: edit, **Save**, **Test sign-in** again, then **Use the new settings**. **Turn SSO off** brings back one-time and email links (everyone's SSO session ends).

### No SSO? Sign in with an email link
If your company has no identity provider, Captain can email a one-time sign-in link instead (no passwords are stored anywhere).
1. Your Captain needs an email sender: `CAPTAIN_SMTP_HOST`, `_PORT`, `_FROM` (and `_USER` / `_PASSWORD`) plus `CAPTAIN_PUBLIC_CONSOLE_URL` in its settings. Ask whoever runs your install.
2. Add each person on **Users** with at least one console role.
3. They open Captain, type their work email and press **Email me a sign-in link**. **Expected:** an email "Your Captain sign-in link"; the link works once, for 15 minutes, and signs them in with the roles you gave them.
4. Deactivating someone on **Users** ends their session at once. At most 3 links per person per 15 minutes; people who are not users get the same answer but no email.
5. Connecting SSO later switches email links off automatically.

## 3. Company details
**Setup → Company** (or the company settings page): name, domains, country, contacts. **Expected:** saved; the checklist item turns green.

## 4. Users
**Users → Add user**: email, name, team, and console roles:
- **org-admin**: everything, including users and AI keys
- **approver**: decides actions that wait for approval
- **finops**: budgets and cost
- **auditor**: evidence and reports
- no role: uses AI through Captain but does not sign in to the dashboard

## 5. AI keys
**AI keys → Add an AI key**: pick the provider, give it a name, paste the key from your provider's website, **Save**, then **Test**. **Expected:** "The key works." The key is stored encrypted and never shown again.

## 6. AI systems, budgets and Captain keys
1. **AI Register**: one entry per AI system or team use (risk tier 1-4, owner, cost centre, allowed models and tools).
2. **Budgets**: a monthly budget for the organisation at least. Calls with no budget are refused.
3. **Captain keys**: one key per person (or per application), tied to an AI system and project. Send each key privately; it is shown once.

## 7. Choose how people will use AI
**Connect your tools**: tick the adopters you will use (VS Code, browser, MCP, SDKs...). Each one has downloads preset for your company, an install guide and a "Test it" list.

## 8. Check it works
1. Ask one person to follow the guide for one adopter and its "Test it" steps.
2. **Activity** shows their request in plain words, for example "ravi@abc.com's request to claude-sonnet was allowed."
3. **Connect your tools** shows calls in the last 7 days for that adopter.
