"""Held-out oracle for the SSHSIG scheme (task 56d9) — the fail-closed matrix.

The implementer does NOT see this file. It pins that ``ssh-keygen -Y verify``
fails closed on tampered bytes, wrong namespace, unknown principal, a substituted
key, and an expired validity window — and that an absent/old ssh-keygen fails
closed rather than silently passing.

Signatures are produced by shelling out to ``ssh-keygen -Y sign`` directly (not
through the module under test), so the verify contract is exercised independently
of the module's own ``sign`` implementation.
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


def _gen_key(tmp_path, name: str):
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key), "-q"],
        check=True,
    )
    pub = (tmp_path / f"{name}.pub").read_text().split()
    return str(key), pub[0], pub[1]  # key_path, key_type, key_b64


def _sign(key_path: str, namespace: str, data: bytes) -> bytes:
    return subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", key_path, "-n", namespace],
        input=data,
        capture_output=True,
        check=True,
    ).stdout


def _allowed_signers(principal: str, key_type: str, key_b64: str, options: str = "") -> str:
    opt = f"{options} " if options else ""
    return f"{principal} {opt}{key_type} {key_b64}\n"


def _verify(scheme, pae_bytes, sig, principal, namespace, allowed_signers):
    return scheme.verify(
        pae_bytes,
        [dsse.Signature(keyid=principal, sig=sig)],
        namespace,
        allowed_signers,
    )


@pytest.fixture
def signed(tmp_path):
    key_path, kt, kb = _gen_key(tmp_path, "id_signer")
    pae_bytes = dsse.pae("t/type", b"canonical attestation bytes")
    sig = _sign(key_path, NAMESPACE, pae_bytes)
    allowed = _allowed_signers(PRINCIPAL, kt, kb)
    return {"pae": pae_bytes, "sig": sig, "allowed": allowed, "key_type": kt, "key_b64": kb}


def test_good_signature_certifies(signed) -> None:
    # Baseline sanity: the fixture's untampered signature verifies.
    v = _verify(
        sshsig.SshsigScheme(), signed["pae"], signed["sig"], PRINCIPAL, NAMESPACE, signed["allowed"]
    )
    assert v.verified is True


def test_tampered_bytes_fail_closed(signed) -> None:
    tampered = signed["pae"] + b"x"
    v = _verify(
        sshsig.SshsigScheme(), tampered, signed["sig"], PRINCIPAL, NAMESPACE, signed["allowed"]
    )
    assert v.verified is False


def test_wrong_namespace_fails_closed(signed) -> None:
    v = _verify(
        sshsig.SshsigScheme(),
        signed["pae"],
        signed["sig"],
        PRINCIPAL,
        "different-namespace",
        signed["allowed"],
    )
    assert v.verified is False


def test_unknown_principal_fails_closed(signed) -> None:
    v = _verify(
        sshsig.SshsigScheme(),
        signed["pae"],
        signed["sig"],
        "nobody@elsewhere",
        NAMESPACE,
        signed["allowed"],
    )
    assert v.verified is False


def test_substituted_key_fails_closed(signed, tmp_path) -> None:
    # allowed_signers lists a DIFFERENT key for the same principal.
    _kp, other_kt, other_kb = _gen_key(tmp_path, "id_other")
    sub_allowed = _allowed_signers(PRINCIPAL, other_kt, other_kb)
    v = _verify(
        sshsig.SshsigScheme(), signed["pae"], signed["sig"], PRINCIPAL, NAMESPACE, sub_allowed
    )
    assert v.verified is False


def test_expired_validity_window_fails_closed(signed) -> None:
    # valid-before in the past -> the signer is no longer trusted at verify time.
    expired_allowed = _allowed_signers(
        PRINCIPAL, signed["key_type"], signed["key_b64"], options="valid-before=20200101"
    )
    v = _verify(
        sshsig.SshsigScheme(), signed["pae"], signed["sig"], PRINCIPAL, NAMESPACE, expired_allowed
    )
    assert v.verified is False


def test_empty_principal_fails_closed(signed) -> None:
    v = _verify(
        sshsig.SshsigScheme(), signed["pae"], signed["sig"], "", NAMESPACE, signed["allowed"]
    )
    assert v.verified is False


def test_ssh_keygen_absent_fails_closed(signed, monkeypatch) -> None:
    # Simulate an environment where ssh-keygen is unavailable/too old: verify must
    # fail closed (no silent pass), never certify.
    monkeypatch.setattr(sshsig, "ssh_keygen_version", lambda: None)
    v = _verify(
        sshsig.SshsigScheme(), signed["pae"], signed["sig"], PRINCIPAL, NAMESPACE, signed["allowed"]
    )
    assert v.verified is False
    assert v.verdict != "certified"


def test_verify_returns_verdict_type(signed) -> None:
    v = _verify(
        sshsig.SshsigScheme(), signed["pae"], signed["sig"], PRINCIPAL, NAMESPACE, signed["allowed"]
    )
    assert isinstance(v, registry.Verdict)


def test_scheme_registered(signed) -> None:
    # register_sshsig_scheme() runs at import; the scheme is discoverable in the registry.
    assert isinstance(registry.get_scheme("sshsig"), sshsig.SshsigScheme)


def test_ensure_available_raises_when_too_old(monkeypatch) -> None:
    # A version below the 8.9 floor must raise SshKeygenUnavailable with a clear message.
    monkeypatch.setattr(sshsig, "ssh_keygen_version", lambda: (8, 1))
    with pytest.raises(sshsig.SshKeygenUnavailable):
        sshsig.ensure_available()


def test_ensure_available_raises_when_absent(monkeypatch) -> None:
    monkeypatch.setattr(sshsig, "ssh_keygen_version", lambda: None)
    with pytest.raises(sshsig.SshKeygenUnavailable):
        sshsig.ensure_available()
