# ADR-003: Signed policy bundles and policy change control

- Status: Proposed
- Date: 2026-11-24
- Code: `backend/captain/policy_bundle.py`, `policies/`, `.github/CODEOWNERS`
- Tests: `backend/tests/integration/test_policy_bundle.py`

## Context
Whoever can change the policy can switch off Captain's controls. The CTO review (R3, F13) requires named signers, the signing key held in KMS, and two-person approval for Tier 1 policy changes before the internal pilot.

## Decision
1. Policies ship as an **OPA bundle signed** with RS256 (`opa build --signing-key`). Test files are excluded.
2. Every OPA instance runs with `--verification-key`. OPA refuses to start, or to activate a downloaded bundle, if the bundle was modified after signing or was signed by any other key. Tests prove both cases against the real OPA 1.0.0 binary, plus a control that the tampered archive is otherwise valid.
3. The bundle carries its version in `data.bundle.version`. Captain records `policy_bundle@policy_version` on every decision and ledger event.
4. Change control:
   - `.github/CODEOWNERS` routes `policies/` changes to the policy-signer team.
   - Branch protection requires two approving reviews.
   - CI builds and signs the bundle only from the protected branch.
5. Signing key:
   - **Development:** a local PEM key.
   - **Production:** KMS, or a signing service the named signers operate. The private key is never on a laptop or in CI logs.
   - **Rotation:** publish the new public key to OPA's verification config first, then sign with the new key.

## Consequences
- A database or OPA host administrator can't quietly weaken a policy. They'd also need the signing key.
- Rolling back means re-deploying an earlier signed bundle. The ledger shows which version made each decision.
- Not covered yet:
  - Separate scopes per tier (OPA `--scope`). Today one bundle holds all tiers, so the "two approvers for Tier 1" rule is enforced by review, not by a separate key.
  - A bundle server or distribution service. For now the bundle is a file.
