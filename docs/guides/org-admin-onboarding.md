# Org admin: from first sign-in to a working setup

You are the first administrator of your company's Captain. Stixis has sent you a one-time sign-in link. This guide takes you from that link to people using AI through Captain. Plan about an hour.

## 1. Sign in
1. Open the link from Stixis. It works once and expires after 7 days.
2. **Expected:** the Captain dashboard opens on **Setup**, with a checklist.

## 2. Connect your company sign-in (SSO)
Do this first: after it, everyone signs in with their normal work account and the one-time link stops working.
1. On **Setup**, follow "Connect your sign-in". You need your identity team for one step (registering Captain as an app in Microsoft Entra ID or Okta).
2. **Expected:** you can sign out, then sign back in with your work account.

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
