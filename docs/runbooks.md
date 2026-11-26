# Captain pilot runbooks

Commands use the Admin API (`/admin/v1/...`, signed in as an Admin) or the dashboard pages named in brackets. Each runbook lists what you should SEE when it worked. Not rehearsed on a real cluster yet; rehearse before the pilot starts.

## 1. A key leaked

1. [Keys] Revoke it: `POST /admin/v1/keys/{key_id}/revoke`. Revocation applies within seconds.
2. Issue a replacement: `POST /admin/v1/keys/{key_id}/rotate` (or create a new key) and hand it over through your secrets channel, never chat or email.
3. [Activity] Filter by that key's principal for the leak window; look for unusual models, volume or refusals.
4. Seen when done: calls with the old key return 401 `AUTHENTICATION`; the revoke is an event in Activity.

## 2. Stop an agent, model, tool or channel NOW (kill switch)

1. [Kill switch] Engage the scope (`POST /admin/v1/killswitch`): channel, model, agent, application, MCP server, or everything.
2. Seen when done: new calls are refused with `KILL_SWITCH_ACTIVE`. In-flight streams finish or fail; they are not cut mid-token.
3. To resume: `POST /admin/v1/killswitch/release` with the same scope. Record why in the reason field.

## 3. Budget exhausted, someone needs more

1. [Budgets] Check the scope (person, project, application, org).
2. Raise it (`PUT /admin/v1/budgets`) or grant a time-boxed override (`POST /admin/v1/budgets/override`). Overrides are events in the ledger.
3. The 80% alert appears under Alerts and goes to `CAPTAIN_ALERT_RECIPIENTS` if SMTP is configured.

## 4. Provider is down or slow

1. Symptom: 502 `PROVIDER_ERROR`, or latency up; the gateway tries each configured route in order.
2. Check the provider's status page first. If one provider is bad, disable its model in the registry (`PUT /admin/v1/registry/models`) so routing uses the next one, or engage the kill switch for that model.
3. Calls that failed before reaching the provider cost nothing (reservations are released).

## 5. Captain itself is unhealthy

| Symptom | Likely cause | Action |
|---|---|---|
| Gateway 503 `CONTROL_UNAVAILABLE` | OPA or the database unreachable. Captain FAILS CLOSED: it refuses rather than guess | Check OPA pod and PostgreSQL; calls resume by themselves |
| Pods not ready | Failed `/health` | `kubectl logs` for the pod; a bad secret or database URL is the usual cause |
| Evidence not appearing | Worker down. Decisions are still recorded in the outbox; sealing resumes when the worker returns | Restart `captain-worker`; keep it at ONE replica |
| Dashboard sign-in fails | SSO misconfigured or user in no mapped group | [docs/sso-setup.md](sso-setup.md); `python -m captain.sso_check` |

## 6. Verify the evidence ledger

1. [Evidence] Run verify, or `POST /admin/v1/ledger/verify`.
2. Seen when OK: chain intact and signatures valid. If it reports a break, STOP and follow break-glass section "Suspected tampering".

## 7. Database restore

Restore PostgreSQL from backup, then run `alembic upgrade head`, then start the worker. Rehearse this BEFORE you need it: `python scripts/restore_drill.py --scratch-url <empty scratch database>` restores a backup into a scratch database and verifies the ledger there (see the script header). After restoring, run ledger verify: events after the restore point are lost from the database; WORM copies (if enabled) are the reference for what was sealed.

**Erasure and backups:** a restore brings back any erasure keys deleted after the backup was taken. After a restore, repeat every erasure recorded since the backup (each is a `subject.erased` event in the ledger; repeat it with the person's id): the restored database has both their key and their records again. Keep backups that contain the `subject_keys` table for at most 30 days.

## 8. Monthly reports

`GET /admin/v1/reports/monthly` and `/admin/v1/reports/chargeback` (also under Reports in the dashboard) produce the compliance and chargeback outputs.

## 9. Erase a person (data-protection request)

Captain never writes a person's id or client IP to the evidence ledger in the clear: it writes a pseudonym (`subj_...`, `ip_...`) and keeps the person's key in `subject_keys` (ADR-005). Erasing deletes that key.

1. Revoke all the person's keys (Keys page). Erasure is refused while they have a usable key, so no new evidence is written under the old pseudonym.
2. `POST /admin/v1/subjects/<principal id>/erase` with `{"reason": "<ticket number, not the person's name>"}` (needs `subjects:erase`, which admins have). The erasure is recorded in the ledger first, with the pseudonym only. **It cannot be undone.**
3. From then on every view and report shows that person's events as **"erased subject"**; totals, chargeback and ledger verification are unchanged.
4. The erasure is complete when the last backup taken before it has expired (at most 30 days, see section 7).

In the same step, every editable record that names the person is changed to the same pseudonym, in one database transaction: the principal record (renamed, display name "erased subject", sign-in link and roles removed, deactivated), their keys (labels cleared), approval tickets, alert history, budgets, rate limits and their counters, and kill switches. Spend is kept under the new name, so totals do not change. A longer id that merely contains theirs (for example `ravi@example.com.au` when erasing `ravi@example.com`) is not touched. The response lists how many records of each kind changed.

Not covered: events written before the 2026-11-26 upgrade hold ids in the clear and cannot be pseudonymised, because the ledger is append-only and signed; and copies outside Captain (e-mails already sent for alerts, ServiceNow incidents, your IdP).

Client IPs are hashed with a key per day; the worker deletes day keys after 30 days, after which those hashes can no longer be linked to anything.
