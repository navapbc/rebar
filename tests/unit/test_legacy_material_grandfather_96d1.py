"""Pre-normalization attestations are grandfathered on read (bug 96d1).

Commit "330c: box flips no longer stale attestations" changed ``material_fingerprint`` to
normalize AC checkbox state before hashing — with no grandfathering. Every completion
attestation signed BEFORE that change embeds ``[x]`` in its hash basis (the 433c close gate
requires all boxes checked at close), so the normalized recompute in ``compute_validity``
can never match and an UNCHANGED ticket reads ``stale-material``. Cascade: an epic whose
children were certified pre-330c closes with ``certifiable=False`` and silently lands
WITHOUT a completion signature (observed on epic 2f4c-7e5c-e782-4e97).

These tests pin the grandfather contract: a signed ``material:`` hash that byte-exactly
matches the LEGACY (non-normalized) recomputation of the CURRENT ticket material is NOT a
material edit — nothing changed, not even box state — so validity holds. A real text edit
still invalidates (the fallback must not weaken staleness detection).
"""

from __future__ import annotations

import hashlib
import json

from rebar.llm.plan_review import attest
from rebar.llm.plan_review.attest import compute_validity

_DESC_CHECKED = (
    "## Approach\nDo the thing.\n\n## Acceptance Criteria\n"
    "- [x] the thing is done\n- [x] the other thing is done\n"
)


def _legacy_hash(ticket_id: str, description: str, file_impact: list | None = None) -> str:
    """The PRE-330c fingerprint: raw description (checkbox state INCLUDED) over the same
    basis shape. Constructed independently of the production code so the oracle does not
    derive from the implementation under test."""
    basis = {
        "ticket_id": ticket_id,
        "description": description,
        "file_impact": file_impact or [],
        "children": [],
    }
    blob = json.dumps(basis, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _wire_state(monkeypatch, state: dict) -> None:
    """Route the REAL fingerprint recomputation at the constructed ticket state (only the
    store read is stubbed; the hashing pipeline runs for real)."""
    import rebar._reads as _reads
    from rebar.llm.plan_review import relation_snapshot

    monkeypatch.setattr(_reads, "show_ticket", lambda tid, repo_root=None: dict(state))
    monkeypatch.setattr(relation_snapshot, "live_material_children", lambda tid, repo_root=None: [])


def test_completion_attestation_signed_pre_normalization_reads_certified(monkeypatch) -> None:
    """An unchanged ticket whose completion attestation was signed under the legacy
    (non-normalized) fingerprint must read valid/certified — not stale-material."""
    state = {"ticket_id": "t-legacy", "status": "closed", "description": _DESC_CHECKED}
    _wire_state(monkeypatch, state)
    signed = _legacy_hash("t-legacy", _DESC_CHECKED)
    att = {"manifest": ["completion-verifier: PASS", f"material: {signed}"], "signed_at": 100}
    res = compute_validity(att, state, "completion-verifier")
    assert res["valid"] is True and res["verdict"] == "certified", res


def test_completion_real_edit_still_stale_material_despite_fallback(monkeypatch) -> None:
    """Negative control: a genuine TEXT edit after signing still invalidates — the legacy
    fallback matches only a byte-identical basis, so it cannot launder a real change."""
    signed = _legacy_hash("t-edited", _DESC_CHECKED)
    edited = _DESC_CHECKED + "\nAnd one more paragraph added after close.\n"
    state = {"ticket_id": "t-edited", "status": "closed", "description": edited}
    _wire_state(monkeypatch, state)
    att = {"manifest": ["completion-verifier: PASS", f"material: {signed}"], "signed_at": 100}
    res = compute_validity(att, state, "completion-verifier")
    assert res["valid"] is False and res["verdict"] == "stale-material", res


def test_plan_review_attestation_signed_pre_normalization_reads_certified(monkeypatch) -> None:
    """The same grandfather applies to the plan-review branch (same seam, same class)."""
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv0")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    state = {"ticket_id": "t-plan", "status": "in_progress", "description": _DESC_CHECKED}
    _wire_state(monkeypatch, state)
    signed = _legacy_hash("t-plan", _DESC_CHECKED)
    att = {
        "manifest": ["plan-review: PASS", f"material: {signed}", "regver: rv0"],
        "head_sha": "headA",
        "signed_at": 100,
    }
    res = compute_validity(att, state, "plan-review")
    assert res["valid"] is True and res["verdict"] == "certified", res


def test_plan_review_real_edit_still_stale_material_despite_fallback(monkeypatch) -> None:
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: "rv0")
    monkeypatch.setattr("rebar.signing.head_sha", lambda repo_root: "headA")
    signed = _legacy_hash("t-plan2", _DESC_CHECKED)
    edited = _DESC_CHECKED.replace("the thing", "a different thing")
    state = {"ticket_id": "t-plan2", "status": "in_progress", "description": edited}
    _wire_state(monkeypatch, state)
    att = {
        "manifest": ["plan-review: PASS", f"material: {signed}", "regver: rv0"],
        "head_sha": "headA",
        "signed_at": 100,
    }
    res = compute_validity(att, state, "plan-review")
    assert res["valid"] is False and res["verdict"] == "stale-material", res


def test_pass_sidecar_payload_carries_certifiable() -> None:
    """The PASS COMPLETION_VERDICT sidecar must record the verdict's ``certifiable`` flag —
    an unsigned certifiable=False close was previously unexplainable from stored data."""
    from rebar.llm import completion_sidecar

    payload = completion_sidecar.build_payload(
        {
            "verdict": "PASS",
            "ticket_id": "x",
            "findings": [],
            "certifiable": False,
        }
    )
    assert payload["schema"] == completion_sidecar.SCHEMA_PASS
    assert payload["certifiable"] is False, payload
