"""PASS-path reuse must REPLAY the last review's stored result (task 167e-e75e-b4e5-468e).

``idempotent_reuse`` (feature b3e5) reused a still-valid attestation but synthesized an
EMPTY verdict — silently dropping the advisory findings and coaching the last real review
surfaced. The BLOCK path (``verdict_reuse``, bug 7e77) already replays the stored sidecar;
these tests close the PASS-path gap:

* stored advisory/coaching/indeterminate + ``coverage.counts`` are replayed when the
  sidecar's ``material_fingerprint`` matches the current one;
* fail-open is preserved — an absent/unreadable/mismatched sidecar (or a stored verdict
  that CONTRADICTS the valid PASS attestation) degrades to the empty-list shape and
  never blocks the reuse;
* both reuse paths stamp a recency anchor (``coverage.replayed_review``) and the CLI
  notation names the output as the LAST review's result replayed against the unchanged
  plan — not a fresh review — rendering the anchor when present, omitting it gracefully.

Kept apart from ``test_plan_review_reuse.py`` (whose fixtures are tuned to the 7e77 BLOCK
end-to-end loop), following the ``test_plan_review_reuse_type.py`` precedent.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rebar.llm.plan_review import reuse

_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
_TS_NS = 1_700_000_000_000_000_000  # 2023-11-14T22:13:20Z


def _ctx(ticket_type: str = "task") -> SimpleNamespace:
    return SimpleNamespace(ticket_id="t-1", ticket_type=ticket_type)


def _pass_sidecar(**over: Any) -> dict[str, Any]:
    """A stored PASS payload whose every reuse precondition holds, with real content
    to replay: one surfaced advisory, one overflow advisory, one dropped-duplicate
    advisory, one indeterminate, and a coaching note."""
    payload: dict[str, Any] = {
        "schema": "plan_review_result_v2",
        "verdict": "PASS",
        "ticket_id": "t-1",
        "ticket_type": "task",
        "material_fingerprint": "fp-unchanged",
        "verified_at_sha": _SHA,
        "coverage": {
            "llm_ran": True,
            "counts": {
                "blocking": 0,
                "advisory_surfaced": 1,
                "advisory_overflow": 1,
                "dropped": 1,
                "indeterminate": 1,
            },
        },
        # Sidecar order is surfaced → overflow → indeterminate → dropped (build_payload).
        "findings": [
            {
                "id": "f-surfaced",
                "decision": "advisory",
                "severity": "minor",
                "criteria": ["C2"],
                "finding": "stored surfaced advisory prose",
            },
            {
                "id": "f-overflow",
                "decision": "advisory",
                "severity": "minor",
                "criteria": ["C3"],
                "finding": "stored overflow advisory prose",
            },
            {
                "id": "f-indet",
                "decision": "indeterminate",
                "criteria": ["F1"],
                "finding": "stored indeterminate prose",
                "reason": "verifier budget exhausted",
            },
            {
                "id": "f-dropped",
                "decision": "advisory",
                "drop_reason": "duplicate",
                "finding": "stored dropped duplicate",
            },
        ],
        "coaching": [
            {
                "move_id": "m1",
                "move_name": "name-the-check",
                "subject": "acceptance criteria",
                "finding_refs": ["f-surfaced"],
                "coaching": "stored coaching prose",
            }
        ],
    }
    payload.update(over)
    return payload


@pytest.fixture
def _pass_reuse_ready(monkeypatch: pytest.MonkeyPatch):
    """Pin every non-sidecar precondition of idempotent_reuse to 'reuse is safe'."""
    monkeypatch.setattr(reuse, "claim_gate_check", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(
        reuse.attest, "current_material_fingerprint", lambda *_a, **_k: "fp-unchanged"
    )
    import rebar.signing as _signing

    monkeypatch.setattr(
        _signing, "verify_signature", lambda *_a, **_k: {"key_id": "k", "head_sha": "h"}
    )
    monkeypatch.setattr(reuse.sidecar, "latest_review_timestamp", lambda *_a, **_k: _TS_NS)


# ---------------------------------------------------------------------------
# AC1: PASS reuse replays the stored result
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pass_reuse_replays_stored_advisory_coaching_and_counts(
    monkeypatch: pytest.MonkeyPatch, _pass_reuse_ready
) -> None:
    monkeypatch.setattr(reuse.sidecar, "latest_review_result", lambda *_a, **_k: _pass_sidecar())
    out = reuse.idempotent_reuse("t-1", _ctx(), repo_root=None)
    assert out is not None and out["verdict"] == "PASS"
    # The stored surfaced advisory is replayed; the stored surfacing cap holds
    # (the overflow finding stays overflow) and a dropped finding is never resurfaced.
    assert [f["id"] for f in out["advisory"]] == ["f-surfaced"]
    assert [f["id"] for f in out["indeterminate"]] == ["f-indet"]
    assert [c["coaching"] for c in out["coaching"]] == ["stored coaching prose"]
    assert out["coverage"]["counts"] == _pass_sidecar()["coverage"]["counts"]
    # The reuse contract is unchanged (b3e5): no LLM, same flags, same signature mirror.
    assert out["coverage"]["llm_ran"] is False
    assert out["coverage"]["idempotent_skip"] is True
    assert out["runner"] == "reused"
    assert out["blocking"] == []
    assert out["signature"]["signed"] is True
    assert out["sidecar_emitted"] is False


@pytest.mark.unit
def test_pass_reuse_stamps_replay_recency_anchor(
    monkeypatch: pytest.MonkeyPatch, _pass_reuse_ready
) -> None:
    monkeypatch.setattr(reuse.sidecar, "latest_review_result", lambda *_a, **_k: _pass_sidecar())
    out = reuse.idempotent_reuse("t-1", _ctx(), repo_root=None)
    assert out is not None
    anchor = out["coverage"]["replayed_review"]
    assert anchor["verified_at_sha"] == _SHA
    assert anchor["reviewed_at"] == _TS_NS


@pytest.mark.unit
def test_reused_pass_and_block_verdicts_carry_sidecar_review_receipts(
    monkeypatch: pytest.MonkeyPatch, _pass_reuse_ready
) -> None:
    """The async gate poller consumes this receipt to prove reused findings are readable.

    ``all_review_results`` annotates each retained sidecar with the filename timestamp;
    both reuse paths must copy that timestamp even though they do not emit a new sidecar.
    """
    monkeypatch.setattr(
        reuse.sidecar, "all_review_results", lambda *_a, **_k: [_pass_sidecar(reviewed_at=_TS_NS)]
    )
    pass_out = reuse.idempotent_reuse("t-1", _ctx(), repo_root=None)

    block_sidecar = {
        "verdict": "BLOCK",
        "ticket_type": "task",
        "material_fingerprint": "fp-unchanged",
        "verified_at_sha": _SHA,
        "findings": [{"decision": "block", "finding": "x", "criteria": ["E2"]}],
        "coaching": [],
        "reviewed_at": _TS_NS,
    }
    monkeypatch.setattr(reuse.sidecar, "all_review_results", lambda *_a, **_k: [block_sidecar])
    monkeypatch.setattr(reuse.sidecar, "review_code_sha", lambda *_a, **_k: _SHA)
    block_out = reuse.verdict_reuse("t-1", _ctx(), repo_root=None)

    assert [
        (pass_out["verdict"], pass_out["sidecar_emitted"], pass_out["sidecar_reviewed_at"]),
        (block_out["verdict"], block_out["sidecar_emitted"], block_out["sidecar_reviewed_at"]),
    ] == [("PASS", False, _TS_NS), ("BLOCK", False, _TS_NS)]


# ---------------------------------------------------------------------------
# fail-open: a sidecar problem must never block a valid PASS reuse
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pass_reuse_fails_open_on_unreadable_sidecar(
    monkeypatch: pytest.MonkeyPatch, _pass_reuse_ready
) -> None:
    def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise OSError("sidecar unreadable")

    monkeypatch.setattr(reuse.sidecar, "latest_review_result", _boom)
    out = reuse.idempotent_reuse("t-1", _ctx(), repo_root=None)
    assert out is not None and out["verdict"] == "PASS"
    assert out["advisory"] == [] and out["coaching"] == [] and out["indeterminate"] == []
    assert out["coverage"]["idempotent_skip"] is True


@pytest.mark.unit
def test_pass_reuse_degrades_to_empty_on_mismatched_fingerprint(
    monkeypatch: pytest.MonkeyPatch, _pass_reuse_ready
) -> None:
    """A sidecar for a DIFFERENT plan revision replays nothing — but the reuse still
    fires (the attestation, not the sidecar, is the validity authority)."""
    monkeypatch.setattr(
        reuse.sidecar,
        "latest_review_result",
        lambda *_a, **_k: _pass_sidecar(material_fingerprint="fp-OTHER"),
    )
    out = reuse.idempotent_reuse("t-1", _ctx(), repo_root=None)
    assert out is not None and out["verdict"] == "PASS"
    assert out["advisory"] == [] and out["coaching"] == [] and out["indeterminate"] == []


@pytest.mark.unit
def test_pass_reuse_never_replays_a_contradictory_stored_block(
    monkeypatch: pytest.MonkeyPatch, _pass_reuse_ready
) -> None:
    """BLOCK stored but the attestation is valid → prefer current behavior: reuse the
    PASS with empty lists rather than replaying contradictory findings."""
    monkeypatch.setattr(
        reuse.sidecar,
        "latest_review_result",
        lambda *_a, **_k: _pass_sidecar(verdict="BLOCK"),
    )
    out = reuse.idempotent_reuse("t-1", _ctx(), repo_root=None)
    assert out is not None and out["verdict"] == "PASS"
    assert out["advisory"] == [] and out["coaching"] == [] and out["indeterminate"] == []


# ---------------------------------------------------------------------------
# the BLOCK path stamps the same recency anchor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_verdict_reuse_stamps_replay_recency_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {
        "verdict": "BLOCK",
        "ticket_type": "task",
        "material_fingerprint": "fp-unchanged",
        "verified_at_sha": _SHA,
        "findings": [{"decision": "block", "finding": "x", "criteria": ["E2"]}],
        "coaching": [],
    }
    monkeypatch.setattr(
        reuse.attest, "current_material_fingerprint", lambda *_a, **_k: "fp-unchanged"
    )
    monkeypatch.setattr(reuse.sidecar, "review_code_sha", lambda *_a, **_k: _SHA)
    monkeypatch.setattr(reuse.sidecar, "latest_review_result", lambda *_a, **_k: stored)
    monkeypatch.setattr(reuse.sidecar, "latest_review_timestamp", lambda *_a, **_k: _TS_NS)
    out = reuse.verdict_reuse("t-1", _ctx(), repo_root=None)
    assert out is not None and out["verdict"] == "BLOCK"
    anchor = out["coverage"]["replayed_review"]
    assert anchor["verified_at_sha"] == _SHA
    assert anchor["reviewed_at"] == _TS_NS


# ---------------------------------------------------------------------------
# CLI notation: name the replay, render the anchor, keep the --force pointer
# ---------------------------------------------------------------------------


def _render(result: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> str:
    from rebar._cli._llm_commands import _render_plan_review_text

    _render_plan_review_text(result)
    return capsys.readouterr().out


@pytest.mark.unit
def test_render_pass_reuse_names_replay_of_last_review(capsys) -> None:
    out = _render(
        {
            "verdict": "PASS",
            "ticket_id": "t",
            "runner": "reused",
            "coverage": {
                "llm_ran": False,
                "idempotent_skip": True,
                "counts": {"advisory_surfaced": 1},
                "replayed_review": {"verified_at_sha": _SHA, "reviewed_at": _TS_NS},
            },
            "blocking": [],
            "advisory": [
                {"criteria": ["C2"], "severity": "minor", "finding": "stored advisory prose"}
            ],
            "coaching": [{"coaching": "stored coaching prose"}],
        },
        capsys,
    )
    assert "reused" in out and "replay" in out.lower()
    assert "--force" in out
    # The replayed findings render in the same slots a fresh review uses.
    assert "stored advisory prose" in out and "stored coaching prose" in out
    # The recency anchor: the stored review's code SHA and timestamp are visible.
    assert _SHA[:12] in out
    assert "2023-11-14" in out


@pytest.mark.unit
def test_render_block_reuse_names_replay_of_last_review(capsys) -> None:
    out = _render(
        {
            "verdict": "BLOCK",
            "ticket_id": "t",
            "runner": "reused",
            "coverage": {
                "llm_ran": False,
                "verdict_reuse": True,
                "counts": {},
                "replayed_review": {"verified_at_sha": _SHA, "reviewed_at": _TS_NS},
            },
            "blocking": [{"criteria": ["P1"], "finding": "no acceptance criteria"}],
            "advisory": [],
            "coaching": [],
        },
        capsys,
    )
    assert "reused" in out and "replay" in out.lower()
    assert "--force" in out
    assert "no acceptance criteria" in out
    assert _SHA[:12] in out and "2023-11-14" in out


@pytest.mark.unit
def test_render_reuse_omits_absent_anchor_gracefully(capsys) -> None:
    """A pre-167e reuse verdict (no replayed_review) still renders — no anchor line."""
    out = _render(
        {
            "verdict": "PASS",
            "ticket_id": "t",
            "runner": "reused",
            "coverage": {"llm_ran": False, "idempotent_skip": True, "counts": {}},
            "blocking": [],
            "advisory": [],
            "coaching": [],
        },
        capsys,
    )
    assert "reused" in out and "--force" in out
    assert "last review:" not in out
