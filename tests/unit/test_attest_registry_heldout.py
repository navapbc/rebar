"""Held-out oracle for the scheme registry (task 518b).

The implementer does NOT see this file. It pins the security-critical contract:
scheme selection comes from the policy table, never the envelope (anti
alg-confusion, RFC 8725); unknown kind/scheme fail closed; the scheme is invoked
over the DSSE-PAE bytes and the policy-pinned namespace.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar.attest import dsse, registry


class _RecordingScheme:
    def __init__(self, name: str, result: registry.Verdict) -> None:
        self.name = name
        self._result = result
        self.calls: list[tuple[bytes, list[dsse.Signature], str, Any]] = []

    def verify(self, pae_bytes, signatures, namespace, trust_root):  # type: ignore[no-untyped-def]
        self.calls.append((pae_bytes, signatures, namespace, trust_root))
        return self._result


@pytest.fixture
def clean_registry():
    schemes_backup = dict(registry._SCHEMES)
    policy_backup = dict(registry.POLICY)
    registry._SCHEMES.clear()
    registry.POLICY.clear()
    try:
        yield
    finally:
        registry._SCHEMES.clear()
        registry._SCHEMES.update(schemes_backup)
        registry.POLICY.clear()
        registry.POLICY.update(policy_backup)


def _envelope(body: bytes = b"payload", keyid: str = "k") -> dsse.Envelope:
    return dsse.decode(dsse.encode("t/type", body, [{"keyid": keyid, "sig": b"s"}]))


# --- The crown jewel: alg-confusion resistance --------------------------------


def test_verify_selects_scheme_from_policy_not_envelope(clean_registry) -> None:
    # Two registered schemes. The policy pins "trusted"; the envelope's in-band
    # keyid names "attacker". verify() MUST dispatch to the policy-pinned scheme
    # and never touch the attacker scheme.
    trusted = _RecordingScheme("trusted", registry.Verdict(True, "certified", "ok"))
    attacker = _RecordingScheme("attacker", registry.Verdict(True, "certified", "pwned"))
    registry.register_scheme(trusted)
    registry.register_scheme(attacker)
    registry.POLICY["k"] = registry.Policy(scheme="trusted", namespace="ns")

    # The envelope carries an in-band hint (keyid) pointing at the attacker scheme.
    env = _envelope(keyid="attacker")
    result = registry.verify("k", env, trust_root=None)

    assert result.verified is True
    assert len(trusted.calls) == 1
    assert attacker.calls == []  # policy chose the scheme, not the envelope


# --- Fail-closed on unknown kind / unknown scheme -----------------------------


def test_unknown_kind_fails_closed(clean_registry) -> None:
    result = registry.verify("no-such-kind", _envelope(), trust_root=None)
    assert result.verified is False
    assert result.verdict == "unknown_kind"


def test_unknown_scheme_fails_closed(clean_registry) -> None:
    # A kind whose policy names a scheme that is not registered.
    registry.POLICY["k2"] = registry.Policy(scheme="not-registered", namespace="ns")
    result = registry.verify("k2", _envelope(), trust_root=None)
    assert result.verified is False
    assert result.verdict == "unknown_scheme"


def test_resolve_unknown_kind_returns_none(clean_registry) -> None:
    assert registry.resolve("no-such-kind") is None


# --- The scheme receives PAE bytes + policy namespace (domain separation) ------


def test_scheme_receives_pae_bytes_and_policy_namespace(clean_registry) -> None:
    scheme = _RecordingScheme("s", registry.Verdict(True, "certified", "ok"))
    registry.register_scheme(scheme)
    registry.POLICY["k"] = registry.Policy(scheme="s", namespace="ns-special")

    env = _envelope(body=b"the-body-bytes")
    registry.verify("k", env, trust_root="TR")

    (pae_bytes, signatures, namespace, trust_root) = scheme.calls[0]
    assert pae_bytes == env.pae()  # scheme verifies over DSSE-PAE bytes
    assert namespace == "ns-special"  # policy-pinned namespace, not envelope-derived
    assert trust_root == "TR"  # caller-supplied trust material passed through
    assert [s.keyid for s in signatures] == ["k"]


# --- A scheme's verification failure propagates (corrupt/wrong signature) ------


def test_scheme_verify_failure_propagates(clean_registry) -> None:
    # Valid kind + correctly-pinned scheme, but the signature does not verify.
    failing = _RecordingScheme("s", registry.Verdict(False, "mismatch", "bad sig"))
    registry.register_scheme(failing)
    registry.POLICY["k"] = registry.Policy(scheme="s", namespace="ns")

    result = registry.verify("k", _envelope(), trust_root=None)
    assert result.verified is False
    assert result.verdict == "mismatch"
