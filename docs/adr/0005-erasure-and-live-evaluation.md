# ADR-005: Erasure (crypto-shredding) and live evaluation

- Status: Accepted 2026-11-26 (decisions below). Live evaluation: built. Erasure: built.
- Date: 2026-11-25
- Related: ADR-003 (signed policy), plan rows C8 (ledger) and "live evaluation" (E1)

Both items change what the evidence ledger holds or how Tier 1-2 calls are judged. Getting either wrong is expensive to undo (the ledger is append-only and signed), so this is a proposal, not an implementation.

## 1. Erasure of a person's data (crypto-shredding)

### Problem
The ledger is hash-chained and signed, so an entry can never be edited or deleted. Events carry a `principal_id` (for people this may be an email-like id) and some `details` (client IP on security events). A data-protection erasure request ("forget this person") cannot be met by deleting rows without breaking the chain.

### What is already true
- Prompt and response content is never stored: events hold decisions, reason codes, counts and finding kinds only.
- Vendor-imported identities that are not mapped are already stored as a short hash, not an email.
- Events carry an optional keyed hash field (`payload_hmac`); nothing writes it yet.

### Proposal
1. Personal identifiers in events are replaced by a **pseudonym**: `HMAC-SHA256(subject_key, identifier)`, truncated, where `subject_key` is a random per-person key held in a separate `subject_keys` table (never in the ledger).
2. The console resolves a pseudonym to a name by looking up the key. Reports and breakdowns work as today.
3. **Erasure = delete that person's key.** Their events stay in the chain (so verification still passes) but can no longer be linked to them, by anyone, including with a database backup taken after the deletion. Backups taken BEFORE the deletion still contain the key; the backup retention policy must say so.
4. Service principals and application ids are not personal data and stay readable.
5. Client IPs on security events are stored as a daily-keyed hash by default.

### Cost and risk
Every place that filters or groups by `principal_id` (usage by person, approvals, observed usage, the Activity filter, chargeback) has to resolve pseudonyms. That is a broad change with real regression risk; it needs its own tests per view.

### DECIDE
- Which identifiers are personal data for your clients (user ids only, or also IPs, device ids)?
- Is a deleted person shown as "erased subject" in reports, or dropped from them?
- Backup retention versus erasure deadlines (key copies live as long as backups).
- Are WORM anchors enough as-is? (They hold hashes of ranges, not identifiers, so yes by design.)

## 2. Live evaluation (in-perimeter judge for Tier 1-2)

### Problem
Guardrails check each request and response against fixed rules. For Tier 1-2 systems the plan also wants an after-the-fact judge: sample governed calls and ask a model whether the answer was grounded, safe and on-policy, and record a score.

### Proposal
1. A worker samples settled Tier 1-2 calls (rate set per register entry, default 5%).
2. It sends the **masked** prompt and response to a judge model **inside the client's perimeter** (the same guard endpoints, `CAPTAIN_GUARD_URLS`, or a dedicated `CAPTAIN_JUDGE_URL`). Content is never sent to a hosted judge unless the client approves a named fallback.
3. The result is recorded as `evaluation.scored` evidence: score, rubric name, judge model version. Never the content.
4. Low scores raise an alert; scores appear per system on the Agents page.

### Why content has to be retained briefly
The judge needs the text, but Captain stores no content. Options: (a) judge inline on a sample of calls before the content is discarded (adds latency to sampled calls), or (b) hold masked content in an encrypted short-lived store (default 24 h) for the worker. Both reduce the "no content stored" guarantee for the sampled calls.

### DECIDE
- Option (a) or (b)?
- Which judge model, and where does it run? (Captain provides the call interface; it cannot supply a model.)
- The rubric: groundedness, policy compliance, safety? Who owns it?
- Sampling rate and the score that raises an alert.

## Decisions (2026-11-26)

| Question | Decision |
|---|---|
| Live evaluation: inline or short-lived store? | **(a) Inline on the sample.** The masked prompt and masked answer are judged while still in memory, after the response has been sent, on a small bounded background pool (the caller does not wait; if the pool is full the sample is skipped and counted). Nothing is stored. |
| Judge model and where it runs | **A dedicated `CAPTAIN_JUDGE_URL`** (OpenAI-compatible, inside the client's perimeter) with `CAPTAIN_JUDGE_MODEL`. Unset = evaluation off. No hosted fallback. |
| Rubric and owner | **Groundedness, policy compliance and safety**, each 0-1; the overall score is the lowest of the three. The rubric is `policies/evaluation/rubric.json`, versioned and reviewed like policy (it is also inside the signed bundle). |
| Sampling and alert | **5% of Tier 1-2 calls** by default, per register entry overrides (`CAPTAIN_EVAL_RATES`); **overall below 0.6** raises an `evaluation.low_score` alert. |
| Erasure: which identifiers are personal | **Person principal ids and client IPs.** Service and application ids stay readable. |
| Erasure: erased person in reports | Shown as **"erased subject"**; their usage and cost stay in the totals. |
| Erasure: backups | **Document plus expiry:** backups that hold subject keys are kept at most 30 days; an erasure is complete 30 days after the key is deleted, and the runbook says so. |

## What was built: live evaluation
- `captain/core/evaluation/` (judge client, rubric, sampling service), wired in the gateway after every successful non-streamed and streamed response (`CaptainCore.evaluate`).
- Evidence: `evaluation.scored` (rubric reference, judge model, each score, overall, low flag, sample rate) and `evaluation.failed` (judge error). Never content.
- Agents page: average score, low scores and judge failures per system.
- Tests: `tests/unit/test_evaluation.py` (sampling, judge client, evidence without content, alert, failure, tier filter, bounded pool, config, and the gateway passing only masked text, streamed and not).

## What was built: erasure
- `captain/core/subjects.py`: the pseudonymiser. Applied in `LedgerService.enqueue`, the one door into the ledger, to `principal_id` and to person fields in event details (actor, approvers, owner, `principal:` budget and `ptool:` rate-limit scopes, person kill-switch targets). A principal is a person unless it is a known service account, application or agent (unknown ids count as people).
- `subject_keys` table (migration 0009), `PgSubjectStore` (race-safe creation; a deleted key is never re-cached).
- Every dashboard read goes through `ResolvingQueries`: people are shown again, or as "erased subject"; a person filter is turned into their pseudonym; erased people merge into one row. The evidence export keeps the stored, signed bodies.
- `POST /admin/v1/subjects/{id}/erase` (`subjects:erase`): refused for services and while the person has a usable key; records `subject.erased` (pseudonym only) before deleting the key.
- Client IPs: `ip_<HMAC(day key, ip)>`; the worker prunes day keys older than 30 days.
- Tests: `tests/unit/test_erasure.py` (every personal field, services untouched, kill targets, IP days and pruning, a person followed through Activity, filters, request detail, coverage, usage by person, chargeback, before and after erasure, with totals and ledger verification unchanged); the whole admin view suite runs a second time with erasure on; `tests/integration/test_postgres.py` checks the key store on PostgreSQL with concurrent writers.
- Editable records (added 2026-11-26): erasure also rewrites the person's id to the same pseudonym in the principal record, keys, approval tickets, alerts, budgets, rate limits and their counters, and kill switches (`captain/core/scrub.py`, `captain/db/scrub.py`, one transaction; whole-token matches only). Tested in memory through the endpoint and on PostgreSQL with a row in every table.
- Limits: events written before this change hold ids in the clear (append-only); copies outside Captain (sent e-mails, ServiceNow incidents, the IdP) are not reachable (runbook section 9).

## Order of work once decided
1. Live evaluation (self-contained, no change to existing events).
2. Erasure (broad; do it with the full views test pass).
