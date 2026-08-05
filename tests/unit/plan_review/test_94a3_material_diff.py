"""Attestation staleness must NAME the material component that changed (bug 94a3).

Before this change every staleness reason recited a fixed list —
``description/AC/file_impact/children`` — that named an input which does not exist ("AC" is
not a basis component; acceptance criteria live inside ``description``) and never said which
input actually differed. Three agents reached contradictory conclusions about whether ticking
an AC checkbox invalidates an attestation precisely because the message could not distinguish
the causes (bugs 846b-1bb5-4cd3-4249, c6c9-cba0-1aa4-47eb and 1909-c1a7-9f20-440f).

These tests pin the diagnostic contract:

* the per-component fingerprints round-trip through the signed manifest;
* a stale-material reason names the differing component(s) and no others;
* ticking a checkbox is attestation-SAFE (the 330c normalization, previously only implicit);
* a pre-330c (raw-description) attestation is not reported as a component edit;
* the gate OUTCOME is unchanged — the same inputs are still refused with the same verdict.
"""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import attest, relation_snapshot
from rebar.llm.plan_review.attest import compute_validity
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.manifest import build_manifest, manifest_material_parts
from rebar.llm.plan_review.material_diff import (
    explain_material_change,
    material_components,
)
from rebar.llm.plan_review.pass1 import material_fingerprint

_UNTICKED = "## Approach\nDo it.\n\n## Acceptance Criteria\n- [ ] alpha\n- [ ] beta\n"
_TICKED = "## Approach\nDo it.\n\n## Acceptance Criteria\n- [x] alpha\n- [X] beta\n"


def _ctx(
    ticket_id: str = "t-94a3",
    description: str = _UNTICKED,
    file_impact: list | None = None,
    children: tuple[str, ...] = (),
) -> PlanContext:
    return PlanContext(
        ticket_id=ticket_id,
        ticket_type="bug",
        title="T",
        description=description,
        state={"ticket_id": ticket_id, "file_impact": file_impact or []},
        children=[{"ticket_id": c} for c in children],
    )


def _state(ctx: PlanContext, *, status: str = "closed") -> dict:
    out = dict(ctx.state)
    out.update({"ticket_id": ctx.ticket_id, "status": status, "description": ctx.description})
    return out


def _wire(monkeypatch, ctx: PlanContext) -> None:
    """Route the REAL recomputation pipeline at ``ctx`` (only the store read is stubbed)."""
    import rebar._reads as _reads

    kids = [{"ticket_id": c.get("ticket_id")} for c in ctx.children]
    monkeypatch.setattr(_reads, "show_ticket", lambda tid, repo_root=None: _state(ctx))
    monkeypatch.setattr(
        relation_snapshot, "live_material_children", lambda tid, repo_root=None: list(kids)
    )


def _attestation(signed_ctx: PlanContext, *, kind: str = "completion-verifier") -> dict:
    """A signed attestation over ``signed_ctx``: the composite plus the component parts."""
    parts = material_components(signed_ctx)
    lines = [f"{kind}: PASS", f"material: {material_fingerprint(signed_ctx)}"]
    lines += [f"material-part: {name} {digest} {size}" for name, (digest, size) in parts.items()]
    return {"manifest": lines, "signed_at": 100}


# ── component fingerprints ──────────────────────────────────────────────────────
def test_component_keys_match_the_fingerprint_basis() -> None:
    """Every basis key of the composite fingerprint has its own component hash."""
    assert set(material_components(_ctx())) == {
        "ticket_id",
        "description",
        "file_impact",
        "children",
    }


def test_file_impact_scope_none_adds_its_own_component() -> None:
    ctx = _ctx()
    ctx.state["file_impact_scope"] = "none"
    ctx.state["no_file_impact_reason"] = "docs only"
    assert "file_impact_scope" in material_components(ctx)


def test_changing_one_component_changes_only_that_component_hash() -> None:
    base = material_components(_ctx())
    edited = material_components(_ctx(description=_UNTICKED + "\nmore prose\n"))
    assert edited["description"][0] != base["description"][0]
    assert edited["file_impact"][0] == base["file_impact"][0]
    assert edited["children"][0] == base["children"][0]


def test_component_hashes_ignore_checkbox_state() -> None:
    """The 330c normalization applies per component, not only to the composite."""
    assert (
        material_components(_ctx(description=_TICKED))["description"][0]
        == (material_components(_ctx(description=_UNTICKED))["description"][0])
    )


# ── manifest round-trip ─────────────────────────────────────────────────────────
def test_build_manifest_round_trips_material_parts() -> None:
    ctx = _ctx(file_impact=[{"path": "a.py"}], children=("c1-1111-2222-3333",))
    parts = material_components(ctx)
    manifest = build_manifest(
        {"verdict": "PASS", "ticket_id": ctx.ticket_id},
        material=material_fingerprint(ctx),
        material_parts=parts,
    )
    assert manifest_material_parts(manifest) == parts


def test_manifest_without_parts_reads_as_empty() -> None:
    assert manifest_material_parts(["plan-review: PASS", "material: abcd"]) == {}


def test_material_part_lines_do_not_confuse_the_material_parser() -> None:
    from rebar.llm.plan_review.manifest import manifest_material

    manifest = ["plan-review: PASS", "material-part: description dead 4", "material: beef"]
    assert manifest_material(manifest) == "beef"


def test_malformed_material_part_line_is_skipped_not_raised() -> None:
    """A diagnostic field must never turn a staleness verdict into a parse error."""
    assert manifest_material_parts(["material-part: bogus"]) == {}


# ── the staleness reason names the component ────────────────────────────────────
@pytest.mark.parametrize(
    ("signed", "current", "named", "not_named"),
    [
        (
            _ctx(),
            _ctx(description=_UNTICKED + "\nnew evidence prose\n"),
            "description",
            ("file_impact", "children"),
        ),
        (
            _ctx(),
            _ctx(file_impact=[{"path": "a.py"}]),
            "file_impact",
            ("description", "children"),
        ),
        (
            _ctx(),
            _ctx(children=("c1-1111-2222-3333",)),
            "children",
            ("description", "file_impact"),
        ),
    ],
)
def test_stale_material_names_the_changed_component(
    monkeypatch, signed, current, named, not_named
) -> None:
    _wire(monkeypatch, current)
    res = compute_validity(_attestation(signed), _state(current), "completion-verifier")
    assert res["valid"] is False
    assert res["verdict"] == "stale-material"
    assert named in res["reason"]
    for other in not_named:
        assert other not in res["reason"]


def test_stale_material_reason_drops_the_fixed_list(monkeypatch) -> None:
    current = _ctx(file_impact=[{"path": "a.py"}])
    _wire(monkeypatch, current)
    res = compute_validity(_attestation(_ctx()), _state(current), "completion-verifier")
    assert "description/AC/file_impact/children" not in res["reason"]


def test_stale_material_reports_before_and_after_sizes(monkeypatch) -> None:
    signed = _ctx(file_impact=[{"path": "a.py"}])
    current = _ctx(file_impact=[{"path": "a.py"}, {"path": "b.py"}, {"path": "c.py"}])
    _wire(monkeypatch, current)
    res = compute_validity(_attestation(signed), _state(current), "completion-verifier")
    assert "1 -> 3" in res["reason"]


def test_the_fixed_enumeration_is_gone_from_the_source_tree() -> None:
    """The misleading list must not survive anywhere but the module that documents its
    removal (it names "AC", which is not a basis component at all)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "src" / "rebar"
    offenders = sorted(
        p.name
        for p in root.rglob("*.py")
        if "description/AC/file_impact/children" in p.read_text(encoding="utf-8")
    )
    assert offenders == ["material_diff.py"]


# ── ticking a checkbox is attestation-SAFE (pins bug 330c) ──────────────────────
def test_ticking_checkboxes_leaves_the_attestation_certified(monkeypatch) -> None:
    """The whole reason 94a3 exists: a tick-only edit must NOT stale an attestation."""
    ticked = _ctx(description=_TICKED)
    _wire(monkeypatch, ticked)
    res = compute_validity(
        _attestation(_ctx(description=_UNTICKED)), _state(ticked), "completion-verifier"
    )
    assert res["valid"] is True
    assert res["verdict"] == "certified"


def test_ticking_plus_added_prose_names_only_description(monkeypatch) -> None:
    """Observation 2 of the report: ticking WITH evidence prose is a description edit."""
    current = _ctx(description=_TICKED + "\nevidence: ran the suite\n")
    _wire(monkeypatch, current)
    res = compute_validity(_attestation(_ctx()), _state(current), "completion-verifier")
    assert res["verdict"] == "stale-material"
    assert "description" in res["reason"]
    assert "children" not in res["reason"]


# ── pre-330c grandfather: no spurious component diff (bug 96d1) ─────────────────
def _legacy_attestation(ctx: PlanContext) -> dict:
    """A PRE-330c attestation: raw-description composite, no component parts."""
    return {
        "manifest": [
            "completion-verifier: PASS",
            f"material: {material_fingerprint(ctx, normalize_checkboxes=False)}",
        ],
        "signed_at": 100,
    }


def test_legacy_attestation_over_unchanged_ticket_stays_certified(monkeypatch) -> None:
    ticked = _ctx(description=_TICKED)
    _wire(monkeypatch, ticked)
    res = compute_validity(_legacy_attestation(ticked), _state(ticked), "completion-verifier")
    assert res["valid"] is True
    assert res["verdict"] == "certified"


def test_pre330c_attestation_over_unticked_boxes_survives_ticking(monkeypatch) -> None:
    """Reconciles observation 1 of the report: a legacy attestation signed while the boxes
    were EMPTY is byte-identical to the normalized basis, so ticking never staled it."""
    ticked = _ctx(description=_TICKED)
    _wire(monkeypatch, ticked)
    res = compute_validity(
        _legacy_attestation(_ctx(description=_UNTICKED)), _state(ticked), "completion-verifier"
    )
    assert res["valid"] is True and res["verdict"] == "certified"


def test_pre94a3_attestation_says_the_component_cannot_be_named(monkeypatch) -> None:
    """Reconciles observation 3: a legacy attestation signed over ALREADY-TICKED boxes goes
    stale when a box is un-ticked, with nothing materially edited. The message must admit it
    cannot name a component rather than blaming one nobody touched."""
    current = _ctx(description=_UNTICKED)
    _wire(monkeypatch, current)
    res = compute_validity(
        _legacy_attestation(_ctx(description=_TICKED)), _state(current), "completion-verifier"
    )
    assert res["verdict"] == "stale-material"
    assert "predates component-level fingerprinting" in res["reason"]
    assert "changed:" not in res["reason"]


def test_describe_delta_is_none_when_nothing_differs() -> None:
    from rebar.llm.plan_review.material_diff import describe_delta

    parts = material_components(_ctx())
    assert describe_delta(parts, dict(parts)) is None


def test_legacy_attestation_with_a_real_edit_still_refuses(monkeypatch) -> None:
    """The degraded path must not weaken staleness detection."""
    current = _ctx(description=_UNTICKED + "\nreal edit\n")
    _wire(monkeypatch, current)
    res = compute_validity(_legacy_attestation(_ctx()), _state(current), "completion-verifier")
    assert res["valid"] is False
    assert res["verdict"] == "stale-material"


# ── explain_material_change never raises ────────────────────────────────────────
def test_explain_is_total_on_an_unreadable_ticket() -> None:
    out = explain_material_change({"manifest": []}, "no-such-ticket-id")
    assert isinstance(out, str) and out


# ── other verdicts name their cause ─────────────────────────────────────────────
def test_stale_reopened_names_both_timestamps(monkeypatch) -> None:
    att = {"manifest": ["completion-verifier: PASS", "material: m1"], "signed_at": 100}
    state = {"ticket_id": "t", "status": "closed", "last_reopened_at": 200}
    res = compute_validity(att, state, "completion-verifier")
    assert res["verdict"] == "stale-reopened"
    assert "100" in res["reason"] and "200" in res["reason"]


def test_unsigned_claim_gate_message_names_both_remedies(monkeypatch) -> None:
    from rebar import signing
    from rebar.llm.plan_review import attest_gate

    monkeypatch.setattr(
        signing,
        "verify_signature",
        lambda tid, kind=None, repo_root=None: {"verified": False, "verdict": "unsigned"},
    )
    res = attest_gate.claim_gate_check("t-94a3")
    assert res["ok"] is False
    assert "review-plan" in res["reason"] and "sign-review" in res["reason"]


# ── gate-outcome invariance ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("state_extra", "verdict"),
    [
        ({"status": "open"}, "not-closed"),
        ({"status": "closed", "last_reopened_at": 200}, "stale-reopened"),
    ],
)
def test_unchanged_verdicts_for_unchanged_inputs(state_extra, verdict) -> None:
    att = {"manifest": ["completion-verifier: PASS", "material: m1"], "signed_at": 100}
    state = {"ticket_id": "t", **state_extra}
    res = compute_validity(att, state, "completion-verifier")
    assert res["valid"] is False
    assert res["verdict"] == verdict


def test_certified_material_match_still_certifies(monkeypatch) -> None:
    ctx = _ctx()
    _wire(monkeypatch, ctx)
    res = compute_validity(_attestation(ctx), _state(ctx), "completion-verifier")
    assert res["valid"] is True and res["verdict"] == "certified"


def test_missing_attestation_still_unsigned() -> None:
    res = compute_validity(None, {"ticket_id": "t"}, "completion-verifier")
    assert res["valid"] is False and res["verdict"] == "unsigned"


def test_current_material_fingerprint_is_unaffected_by_component_hashing(monkeypatch) -> None:
    """The composite must be byte-identical to the pre-change algorithm."""
    import hashlib
    import json

    ctx = _ctx(file_impact=[{"path": "a.py"}], children=("c1-1111-2222-3333",))
    basis = {
        "ticket_id": ctx.ticket_id,
        "description": _UNTICKED,
        "file_impact": [{"path": "a.py"}],
        "children": ["c1-1111-2222-3333"],
    }
    blob = json.dumps(basis, sort_keys=True, ensure_ascii=False)
    assert material_fingerprint(ctx) == hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def test_dependency_material_change_names_the_dependency_ids() -> None:
    """Parity with `resign`, which already names the changed dependency ids."""
    from rebar.llm.plan_review.generation import _related_material_delta
    from rebar.llm.plan_review.relation_snapshot import PlanMaterialPin

    a = PlanMaterialPin("child", "aaaa-1111-2222-3333", "0" * 16)
    b = PlanMaterialPin("child", "bbbb-1111-2222-3333", "1" * 16)
    a_moved = PlanMaterialPin("child", "aaaa-1111-2222-3333", "2" * 16)

    assert _related_material_delta((a, b), (a,)) == "bbbb-1111-2222-3333"
    assert _related_material_delta((a_moved,), (a,)) == "aaaa-1111-2222-3333"
    assert _related_material_delta((a,), (a,)) == "unknown"


def test_sign_aborted_names_its_reason_and_refuses_to_advise_resign(capsys) -> None:
    """`sign-review` re-collects the same unreadable state, so advising it for a
    store-read-failure sends the reader in a circle."""
    from rebar._cli._llm_commands import _disposition_exit_code

    code = _disposition_exit_code(
        {
            "verdict": "PASS",
            "ticket_id": "t-94a3",
            "signature": {
                "signed": False,
                "error": "store-read-failure",
                "event": "plan_review_sign_aborted",
            },
        },
        indeterminate_code=2,
    )
    err = capsys.readouterr().err
    assert code == 11
    assert "store-read-failure" in err
    assert "`rebar sign-review t-94a3` to re-sign" not in err
    assert "review-plan t-94a3" in err


def test_other_sign_aborts_keep_the_resign_advice(capsys) -> None:
    """`plan_review_sign_aborted` is the base-class event and also covers arbitrary terminal
    signing errors, where `sign-review` IS the right recovery. Only relation-READ failures
    lose that advice."""
    from rebar._cli._llm_commands import _disposition_exit_code

    _disposition_exit_code(
        {
            "verdict": "PASS",
            "ticket_id": "t-94a3",
            "signature": {
                "signed": False,
                "error": "OSError: disk full",
                "event": "plan_review_sign_aborted",
            },
        },
        indeterminate_code=2,
    )
    assert "sign-review t-94a3" in capsys.readouterr().err


def test_transient_sign_failure_still_advises_resign(capsys) -> None:
    """The cheap recovery must survive: only the ABORT branch changes."""
    from rebar._cli._llm_commands import _disposition_exit_code

    _disposition_exit_code(
        {
            "verdict": "PASS",
            "ticket_id": "t-94a3",
            "signature": {
                "signed": False,
                "error": "index.lock exists",
                "event": "plan_review_generation_retry",
            },
        },
        indeterminate_code=2,
    )
    assert "sign-review t-94a3" in capsys.readouterr().err


def test_attest_module_exposes_no_stale_helper_regression() -> None:
    """The public seam the close gate patches must keep working."""
    assert callable(attest.current_material_fingerprint)
