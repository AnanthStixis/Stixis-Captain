> Sample generated from the local development database (mock and test traffic), only to show the format.

# Stixis Captain: pilot report
Period: last 14 days (2026-11-11 to 2026-11-25)

## Volume
- Governed decisions: **631** (refused: 69, 10.9%)
- Observed-only events: 18; bypass signals: 0
- Approvals pending now: 2; refusals not tied to a system (bad key, retired system): 3

| Channel | GOVERNED | OBSERVED | BYPASS_DETECTED |
|---|---|---|---|
| API | 586 | 0 | 0 |
| BROWSER | 5 | 0 | 0 |
| HOOK | 4 | 4 | 0 |
| IDE | 8 | 14 | 0 |
| MCP | 25 | 0 | 0 |

## Why calls were refused
- MODEL_NOT_REGISTERED: 19
- KILL_SWITCH_ACTIVE: 15
- CONTROL_UNAVAILABLE: 12
- BUDGET_EXHAUSTED: 9
- MODEL_NOT_ALLOWED: 7
- AUTH_INVALID: 3
- TOOL_NOT_REGISTERED: 3
- RATE_LIMITED: 1

## Cost
- Spend: **$0.36** ($0.00 of it estimated, not provider-reported)
- Tokens: 156,317 in / 3,751 out
- claude-sonnet: $0.21
- vscode: $0.16

Top people by spend: dev-app ($0.36), dev-agent ($0.00), dev-coder ($0.00)

Top applications by spend: dev-application ($0.36)

## Latency (Captain + model)
- Total call time: p50 155 ms, p95 250 ms
- Time to first token (streamed): p50 93 ms, p95 1305 ms

## MVP acceptance (M1a)
| Criterion | Status | Evidence |
|---|---|---|
| Cap refusal with a clear reason | MET | 9 BUDGET_EXHAUSTED refusals in the period |
| 80% budget alert | NOT SEEN | 0 budget.threshold alerts in the period |
| Streamed workload: time to first token measured | MET | p50 93 ms |
| Chargeback produced automatically | MET | GET /admin/v1/reports/chargeback (also on the Budgets page); not time-dependent |
| SSO and roles restrict access | CHECK ELSEWHERE | run `python -m captain.sso_check` and sign in as a user in each role (docs/sso-setup.md) |
| 100% of pilot calls go through Captain; no developer-held provider keys | CHECK ELSEWHERE | needs the secret scan and the egress test (cluster); Captain saw 0 bypass signals from discovery logs |
| Latency within target at pilot load | CHECK ELSEWHERE | run `python -m bench.gateway_load` at pilot concurrency; the figures above include model time |
| Pilot metrics reported | MET | this report |

MET = seen in the data. NOT SEEN = no example in this period yet. CHECK ELSEWHERE = cannot be measured from Captain's own ledger.
