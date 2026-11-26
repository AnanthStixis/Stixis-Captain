# Break-glass procedure

Break-glass is the exceptional access that bypasses normal controls: it exists so a broken or compromised Captain can be repaired, and it is designed to be LOUD and AFTER-THE-FACT reviewable. This is a procedure to adopt, not an automated feature: the roles and the client-notification step need your organisation's decisions where marked. The approver and notification rules below are DEFAULTS chosen so you can start; replace them when your contracts and org chart say otherwise.

## What counts

| Access | Who | Normal path instead |
|---|---|---|
| Database OWNER role (the migrating role) | Platform on-call, two people | Application role: can only INSERT/SELECT the ledger |
| Admin API outside SSO | Nobody. There is no local admin login | Fix SSO (docs/sso-setup.md); do not add a back door |
| Stixis operator access to a client deployment | Stixis operator | Client-run Admin sign-in |

The ledger table is append-only for the application role; only the owning role can alter it. That is why owner access is the break-glass role, and why any change it makes to the ledger is detectable (the hash chain and anchors break).

## Procedure

1. **Declare.** Open an incident record with: who, why, which system, expected duration. A second approver (not the requester) approves in writing. Default until your organisation decides: the approver is the engineering lead, with the CTO as backup; the requester can never approve their own request.
2. **Get access** through your secrets manager, time-boxed (hours, not days). Never share the owner credential in chat or email.
3. **Do the minimum**, ideally with a second person watching. Prefer Admin API actions (kill switch, revoke key) over direct database edits.
4. **Revoke.** Rotate the owner credential afterwards.
5. **Verify evidence.** Run ledger verify (runbook 6). If you did not touch the ledger it must pass. Note the result in the incident.
6. **Notify the client** for any Stixis operator access to a client deployment: what, when, why, result. Default until the client contract says otherwise: email to the client's named security contact within 24 hours of the access ending.
7. **Review** within 2 working days: was it needed, could a normal control have done it, what should change.

## Suspected tampering

If ledger verify reports a break: stop making changes, preserve the database and any WORM copies, compare against the last signed anchor / WORM copy to find the first bad event, and treat it as a security incident under your incident process. Do not "repair" the chain.
