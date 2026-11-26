"""Signed OPA bundles with a real local OPA binary (no PostgreSQL needed)."""

from __future__ import annotations

import io
import subprocess
import tarfile
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from captain.policy_bundle import build_signed_bundle
from tests.integration.test_core_real import REPO, _free_port, _opa_bin

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def keys(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    d = tmp_path_factory.mktemp("keys")
    out = []
    for name in ("signer", "attacker"):
        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv, pub = d / f"{name}.pem", d / f"{name}.pub.pem"
        priv.write_bytes(
            k.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        pub.write_bytes(
            k.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        out += [priv, pub]
    return out[0], out[1], out[2]  # signer key, signer pub, attacker key


@pytest.fixture(scope="module")
def opa() -> str:
    b = _opa_bin()
    if not b:
        pytest.skip("OPA binary not found")
    return b


def _serve(opa: str, bundle: Path, pub: Path | None) -> tuple[subprocess.Popen[bytes], str | None]:
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603
        [
            opa,
            "run",
            "--server",
            f"--addr=127.0.0.1:{port}",
            "--log-level=error",
            "--bundle",
            bundle.name,
            *(
                ["--verification-key", str(pub), "--signing-alg", "RS256"]
                if pub
                else ["--skip-verify"]
            ),
        ],
        cwd=bundle.parent,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return proc, None  # OPA refused to start
        try:
            if httpx.get(f"{url}/health?bundles", timeout=0.5).status_code == 200:
                return proc, url
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    proc.kill()
    return proc, None


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=10)


def test_signed_bundle_serves_decisions(
    opa: str, keys: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    signer, pub, _ = keys
    bundle = build_signed_bundle(opa, REPO / "policies", signer, tmp_path / "bundle.tar.gz")
    with tarfile.open(bundle) as t:
        names = t.getnames()
    assert any(n.endswith(".signatures.json") for n in names)
    assert not any(n.endswith("_test.rego") for n in names)  # tests are not shipped
    proc, url = _serve(opa, bundle, pub)
    try:
        assert url is not None, "OPA refused a correctly signed bundle"
        inp = {
            "input": {
                "request": {
                    "action": "MODEL_CALL",
                    "coverage": "GOVERNED",
                    "model": "m",
                    "tool": None,
                    "max_output_tokens": 10,
                },
                "register": {
                    "id": "AI-1",
                    "tier": "TIER_4",
                    "autonomy": "A0",
                    "allowed_models": ["m"],
                    "allowed_tools": [],
                },
                "findings": [],
            }
        }
        r = httpx.post(f"{url}/v1/data/captain/decision/result", json=inp).json()
        assert r["result"]["decision"] == "ALLOW"
    finally:
        _stop(proc)


def test_tampered_bundle_is_refused(
    opa: str, keys: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    signer, pub, _ = keys
    good = build_signed_bundle(opa, REPO / "policies", signer, tmp_path / "good.tar.gz")
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(good) as src, tarfile.open(bad, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read() if m.isfile() else None  # type: ignore[union-attr]
            if data is not None and m.name.endswith("decision.rego"):
                # Attacker weakens the policy: Tier 1 injection now only logged.
                data = data.replace(b'tier.injection == "block"', b'tier.injection == "never"')
                m.size = len(data)
            dst.addfile(m, io.BytesIO(data) if data is not None else None)
    proc, url = _serve(opa, bad, pub)
    _stop(proc)
    assert url is None, "OPA served a tampered bundle"
    # Control: the archive itself is valid; only signature verification rejects it.
    proc, url = _serve(opa, bad, None)
    _stop(proc)
    assert url is not None


def test_bundle_signed_with_wrong_key_is_refused(
    opa: str, keys: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, pub, attacker = keys
    forged = build_signed_bundle(opa, REPO / "policies", attacker, tmp_path / "forged.tar.gz")
    proc, url = _serve(opa, forged, pub)
    _stop(proc)
    assert url is None, "OPA served a bundle signed by an untrusted key"
