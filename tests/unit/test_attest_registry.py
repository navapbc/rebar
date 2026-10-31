"""Happy-path spec for the scheme registry + verify-dispatch (task 518b).

Edge cases (alg-confusion, fail-closed, namespace/PAE passthrough) live in the
held-out oracles below.

Contract (observable behavior only):

* ``register_scheme(scheme)`` / ``get_scheme(name)`` register and look up schemes.
* ``resolve(kind)`` returns the pinned ``Policy`` for a kind (or ``None``).
* ``verify(kind, envelope, trust_root)`` dispatches to the policy-pinned scheme,
  passing the envelope's PAE bytes, its signatures, the policy namespace, and the
  caller's ``trust_root``, and returns the scheme's ``Verdict``.

Held-out oracles merged from test_attest_registry_heldout.py: Held-out oracle for the scheme
registry (task 518b).
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar.attest import dsse, registry


class _RecordingScheme:
    """A test scheme that records how it was invoked and returns a fixed verdict."""

    def __init__(self, name: str, result: registry.Verdict) -> None:
        self.name = name
        self._result = result
        self.calls: list[tuple[bytes, list[dsse.Signature], str, Any]] = []

    def verify(self, pae_bytes, signatures, namespace, trust_root):  # type: ignore[no-untyped-def]
        self.calls.append((pae_bytes, signatures, namespace, trust_root))
        return self._result


@pytest.fixture
def clean_registry():
    """Snapshot and restore the module-global registry + policy table."""
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


def _envelope(body: bytes = b"payload") -> dsse.Envelope:
    return dsse.decode(dsse.encode("t/type", body, [{"keyid": "k", "sig": b"s"}]))


def test_register_and_get_scheme(clean_registry) -> None:
    scheme = _RecordingScheme("good", registry.Verdict(True, "certified", "ok"))
    registry.register_scheme(scheme)
    assert registry.get_scheme("good") is scheme
    assert registry.get_scheme("missing") is None


def test_resolve_returns_pinned_policy(clean_registry) -> None:
    registry.POLICY["kindX"] = registry.Policy(scheme="good", namespace="ns-x")
    assert registry.resolve("kindX") == registry.Policy(scheme="good", namespace="ns-x")


def test_verify_dispatches_to_policy_scheme(clean_registry) -> None:
    good = _RecordingScheme("good", registry.Verdict(True, "certified", "ok"))
    registry.register_scheme(good)
    registry.POLICY["kindX"] = registry.Policy(scheme="good", namespace="ns-x")

    result = registry.verify("kindX", _envelope(), trust_root="TR")

    assert result.verified is True
    assert result.verdict == "certified"
    assert len(good.calls) == 1


# ── held-out oracles (merged from test_attest_registry_heldout.py) ──


class _RecordingScheme_heldout:
    def __init__(self, name: str, result: registry.Verdict) -> None:
        self.name = name
        self._result = result
        self.calls: list[tuple[bytes, list[dsse.Signature], str, Any]] = []

    def verify(self, pae_bytes, signatures, namespace, trust_root):  # type: ignore[no-untyped-def]
        self.calls.append((pae_bytes, signatures, namespace, trust_root))
        return self._result


@pytest.fixture
def clean_registry_heldout():
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


def _envelope_heldout(body: bytes = b"payload", keyid: str = "k") -> dsse.Envelope:
    return dsse.decode(dsse.encode("t/type", body, [{"keyid": keyid, "sig": b"s"}]))


# --- The crown jewel: alg-confusion resistance --------------------------------


def test_verify_selects_scheme_from_policy_not_envelope(clean_registry_heldout) -> None:
    # Two registered schemes. The policy pins "trusted"; the envelope's in-band
    # keyid names "attacker". verify() MUST dispatch to the policy-pinned scheme
    # and never touch the attacker scheme.
    trusted = _RecordingScheme_heldout("trusted", registry.Verdict(True, "certified", "ok"))
    attacker = _RecordingScheme_heldout("attacker", registry.Verdict(True, "certified", "pwned"))
    registry.register_scheme(trusted)
    registry.register_scheme(attacker)
    registry.POLICY["k"] = registry.Policy(scheme="trusted", namespace="ns")

    # The envelope carries an in-band hint (keyid) pointing at the attacker scheme.
    env = _envelope_heldout(keyid="attacker")
    result = registry.verify("k", env, trust_root=None)

    assert result.verified is True
    assert len(trusted.calls) == 1
    assert attacker.calls == []  # policy chose the scheme, not the envelope


# --- Fail-closed on unknown kind / unknown scheme -----------------------------


def test_unknown_kind_fails_closed(clean_registry_heldout) -> None:
    result = registry.verify("no-such-kind", _envelope_heldout(), trust_root=None)
    assert result.verified is False
    assert result.verdict == "unknown_kind"


def test_unknown_scheme_fails_closed(clean_registry_heldout) -> None:
    # A kind whose policy names a scheme that is not registered.
    registry.POLICY["k2"] = registry.Policy(scheme="not-registered", namespace="ns")
    result = registry.verify("k2", _envelope_heldout(), trust_root=None)
    assert result.verified is False
    assert result.verdict == "unknown_scheme"


def test_resolve_unknown_kind_returns_none(clean_registry_heldout) -> None:
    assert registry.resolve("no-such-kind") is None


# --- The scheme receives PAE bytes + policy namespace (domain separation) ------


def test_scheme_receives_pae_bytes_and_policy_namespace(clean_registry_heldout) -> None:
    scheme = _RecordingScheme_heldout("s", registry.Verdict(True, "certified", "ok"))
    registry.register_scheme(scheme)
    registry.POLICY["k"] = registry.Policy(scheme="s", namespace="ns-special")

    env = _envelope_heldout(body=b"the-body-bytes")
    registry.verify("k", env, trust_root="TR")

    (pae_bytes, signatures, namespace, trust_root) = scheme.calls[0]
    assert pae_bytes == env.pae()  # scheme verifies over DSSE-PAE bytes
    assert namespace == "ns-special"  # policy-pinned namespace, not envelope-derived
    assert trust_root == "TR"  # caller-supplied trust material passed through
    assert [s.keyid for s in signatures] == ["k"]


# --- A scheme's verification failure propagates (corrupt/wrong signature) ------


def test_scheme_verify_failure_propagates(clean_registry_heldout) -> None:
    # Valid kind + correctly-pinned scheme, but the signature does not verify.
    failing = _RecordingScheme_heldout("s", registry.Verdict(False, "mismatch", "bad sig"))
    registry.register_scheme(failing)
    registry.POLICY["k"] = registry.Policy(scheme="s", namespace="ns")

    result = registry.verify("k", _envelope_heldout(), trust_root=None)
    assert result.verified is False
    assert result.verdict == "mismatch"
