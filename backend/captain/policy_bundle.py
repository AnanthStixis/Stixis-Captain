"""Build a signed OPA policy bundle (CTO R3: policy signing and change control).

    python -m captain.policy_bundle --policies ../policies --key signing.pem --out bundle.tar.gz

OPA then runs with signature verification, refusing any bundle that was modified
after signing or was never signed:

    opa run --server --bundle bundle.tar.gz --verification-key public.pem \
        --verification-key-id captain-policy --signing-alg RS256

Production: the signing key lives in KMS / a signing service operated by named policy
signers, never on a developer laptop; CI builds the bundle only after the required
reviews (two approvers for Tier 1 changes — see .github/CODEOWNERS). Test files
(*_test.rego) are excluded from the shipped bundle.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

KEY_ID = "captain-policy"


def build_signed_bundle(
    opa_bin: str, policies_dir: Path, signing_key: Path, out: Path, alg: str = "RS256"
) -> Path:
    # Run from the policies' parent with a relative path: on Windows OPA reads
    # "C:\\..." as "<prefix>:<path>".
    cmd = [
        opa_bin, "build", "--bundle", policies_dir.name,
        "--signing-key", str(signing_key.resolve()), "--signing-alg", alg,
        "--ignore", "*_test.rego",
        "--output", str(out.resolve()),
    ]  # fmt: skip
    res = subprocess.run(cmd, cwd=policies_dir.resolve().parent, capture_output=True, text=True)  # noqa: S603
    if res.returncode != 0:
        raise RuntimeError(f"opa build failed: {res.stderr.strip() or res.stdout.strip()}")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--policies", type=Path, required=True)
    p.add_argument("--key", type=Path, required=True, help="PEM private key (RSA or ECDSA)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--alg", default="RS256")
    p.add_argument("--opa", default=shutil.which("opa") or "opa")
    a = p.parse_args()
    print(build_signed_bundle(a.opa, a.policies, a.key, a.out, a.alg))


if __name__ == "__main__":
    main()
