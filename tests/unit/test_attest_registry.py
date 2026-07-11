"""Happy-path spec for the scheme registry + verify-dispatch (task 518b).

Edge cases (alg-confusion, fail-closed, namespace/PAE passthrough) live in the
held-out companion ``test_attest_registry_heldout.py``.

Contract (observable behavior only):

* ``register_scheme(scheme)`` / ``get_scheme(name)`` register and look up schemes.
* ``resolve(kind)`` returns the pinned ``Policy`` for a kind (or ``None``).
* ``verify(kind, envelope, trust_root)`` dispatches to the policy-pinned scheme,
  passing the envelope's PAE bytes, its signatures, the policy namespace, and the
  caller's ``trust_root``, and returns the scheme's ``Verdict``.
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
