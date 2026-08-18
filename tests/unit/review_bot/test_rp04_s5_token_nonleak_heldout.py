"""HELD-OUT edge oracle for RP-04 S5 (5851) — AC2 MCP token non-leakage.

The implementer does NOT see this file. It asserts the OBSERVABLE contract that inbound
MCP bearer/OAuth material can NEVER enter a composed non-secret ``OperationSnapshot``:
the validating constructor rejects secret-typed / non-JSON leaves, and a snapshot composed
for an operation carries no ambient bearer token in its canonical bytes.

Run: copy into ``tests/unit/review_bot/`` as ``test_rp04_s5_token_nonleak_heldout.py``.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from rebar._operation_config import OperationSnapshot, compose_operation_snapshot


def test_operation_snapshot_rejects_secret_typed_material() -> None:
    """The non-secret snapshot's validating constructor refuses a secret-typed leaf, so an
    inbound bearer object cannot be smuggled into a snapshot section."""
    with pytest.raises((TypeError, ValueError)):
        OperationSnapshot.build(
            envelope_version=1,
            repo_root="/tmp/repo",
            values={"auth": {"bearer": SecretStr("inbound-token-abc123")}},
            sources={"auth": {"bearer": "default"}},
        )


def test_composed_snapshot_carries_no_inbound_bearer_token(tmp_path, monkeypatch) -> None:
    """A snapshot composed for an operation is config-only: even with an inbound bearer
    token loose in the ambient environment, it never appears in the snapshot's canonical
    serialization (the wire/committed form) nor its document."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".rebar").mkdir()
    monkeypatch.setenv("AUTHORIZATION", "Bearer inbound-secret-tok-zzz")
    monkeypatch.setenv("MCP_INBOUND_BEARER", "inbound-secret-tok-zzz")

    snap = compose_operation_snapshot(repo_root=str(tmp_path))

    blob = snap.canonical_bytes()
    assert b"inbound-secret-tok-zzz" not in blob
    assert b"Bearer" not in blob
    doc = snap.canonical_document()
    assert "inbound-secret-tok-zzz" not in repr(doc)
