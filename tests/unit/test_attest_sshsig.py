"""Happy-path spec for the SSHSIG scheme (task 56d9).

The fail-closed matrix (tampered bytes, wrong namespace, unknown principal,
substituted key, expired window) and the ssh-keygen-absent guard live in the
held-out companion ``test_attest_sshsig_heldout.py``.

These are real integration tests against ``ssh-keygen`` (no new dependency).
They skip only where ssh-keygen >= 8.9 is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from rebar.attest import dsse, registry, sshsig

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None,
    reason="ssh-keygen not available",
)

NAMESPACE = "rebar-attest"
PRINCIPAL = "attester@rebar.test"


def _keypair(tmp_path):
    key = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "attest-test", "-f", str(key), "-q"],
        check=True,
    )
    pub = (tmp_path / "id_ed25519.pub").read_text().split()
    allowed_signers = tmp_path / "allowed_signers"
    allowed_signers.write_text(f"{PRINCIPAL} {pub[0]} {pub[1]}\n")
    return str(key), allowed_signers.read_text()


def test_ssh_keygen_version_detected() -> None:
    ver = sshsig.ssh_keygen_version()
    assert ver is not None
    assert isinstance(ver, tuple) and len(ver) == 2
    assert ver >= sshsig.SSHSIG_MIN_VERSION


def test_sshsig_sign_and_verify_roundtrip(tmp_path) -> None:
    key_path, allowed_signers = _keypair(tmp_path)
    pae_bytes = dsse.pae("t/type", b"the exact attestation bytes")

    sig = sshsig.sign(pae_bytes, key_path, NAMESPACE)
    assert isinstance(sig, bytes) and sig  # a non-empty armored signature

    envelope_sigs = [dsse.Signature(keyid=PRINCIPAL, sig=sig)]
    verdict = sshsig.SshsigScheme().verify(pae_bytes, envelope_sigs, NAMESPACE, allowed_signers)

    assert isinstance(verdict, registry.Verdict)
    assert verdict.verified is True
    assert verdict.verdict == "certified"
