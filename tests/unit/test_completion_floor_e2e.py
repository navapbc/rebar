"""End-to-end completion-floor behaviour over a partially-complete epic (epic 66ac / story 77cf).

Builds a partial epic — DELIVERED (closed + attested) + OPEN + FORCE-CLOSED children — assembles the
delivered-children manifest through the real ``delivered_children_manifest`` (rebar reads mocked),
then drives the Pass-3 completion floor with the gold set's CORRECT sub-answers and asserts every
anchor's expected drop/keep. Also pins: byte-identical behaviour with the floor inactive; the
reopen auto-resurface invariant (recompute-every-run, no persisted drop); and the epic's
non-regression invariants (no close-op change, no new attestation kind).
"""

from __future__ import annotations

import pytest
from gold_set_completion import (
    CATEGORIES,
    DELIVERED_CHILD_IDS,
    GOLD_SET,
    PROVENANCES,
)

import rebar
from rebar.llm import plan_review
from rebar.llm.plan_review import attest, orchestrator

pytestmark = pytest.mark.unit

_FLOOR = 0.4
_PRESERVE = frozenset({"T5c", "T10"})


# ── the gold set is well-formed (AC7: >=5 per category, >=25 total, all provenances) ────────────
def test_gold_set_shape() -> None:
    assert len(GOLD_SET) >= 25
    by_cat: dict[str, int] = {c: 0 for c in CATEGORIES}
    seen_prov: set[str] = set()
    for case in GOLD_SET:
        assert case.category in CATEGORIES
        assert case.provenance in PROVENANCES
        by_cat[case.category] += 1
        seen_prov.add(case.provenance)
        # every non-DROP category is a must-never-suppress anchor
        assert case.expect_drop == (case.category == "DROP")
    assert all(n >= 5 for n in by_cat.values()), by_cat
    assert seen_prov == set(PROVENANCES)  # all three provenances represented


# ── build a partially-complete epic + assemble the delivered manifest ───────────────────────────
_AC = "## Acceptance Criteria\n- [ ] it works\n- [ ] it is verified\n"


def _partial_epic(monkeypatch, *, delivered=DELIVERED_CHILD_IDS) -> None:
    """Mock the store so the epic has delivered (closed+attested), open, and force-closed children.
    ``delivered_now`` is truthy only for ids in ``delivered`` — so the manifest (and thus the
    droppable set) is exactly those."""
    children = [
        {"ticket_id": "del-a", "status": "closed", "description": _AC},
        {"ticket_id": "del-b", "status": "closed", "description": _AC},
        {"ticket_id": "del-c", "status": "closed", "description": _AC},
        {"ticket_id": "op-x", "status": "open", "description": _AC},  # open sibling
        {"ticket_id": "fc1", "status": "closed", "description": _AC},  # force-closed (unsigned)
    ]
    monkeypatch.setattr("rebar._reads.list_tickets", lambda *, parent, repo_root=None: children)
    monkeypatch.setattr(
        "rebar._reads.show_ticket",
        lambda cid, repo_root=None: next(c for c in children if c["ticket_id"] == cid),
    )
    monkeypatch.setattr(
        attest,
        "delivered_now",
        lambda child, siblings, repo_root=None: child.get("ticket_id") in delivered,
    )


def _delivered_ids(repo_root=None) -> frozenset[str]:
    manifest = orchestrator.delivered_children_manifest("epic", repo_root=repo_root)
    return frozenset(m["ticket_id"] for m in manifest if m.get("ticket_id"))


def test_manifest_excludes_open_and_force_closed(monkeypatch) -> None:
    _partial_epic(monkeypatch)
    assert _delivered_ids() == DELIVERED_CHILD_IDS  # op-x (open) + fc1 (force-closed) excluded


# ── the CORE: drive the floor with gold labels over the partial epic ────────────────────────────
def _verdict_from_gold() -> dict:
    # every finding priced BELOW the floor, so only the classification axes + preserve + delivered
    # decide the drop — isolating what this story calibrates.
    return {
        "verdict": "PASS",
        "advisory": [
            {"id": c.id, "priority": 0.1, "criteria": c.finding["criteria"]} for c in GOLD_SET
        ],
        "dropped": [],
        "coverage": {"counts": {"advisory_surfaced": len(GOLD_SET), "dropped": 0}},
    }


def _gold_map() -> dict:
    return {i: c.gold for i, c in enumerate(GOLD_SET)}


def test_floor_drops_and_preserves_per_gold_labels(monkeypatch) -> None:
    _partial_epic(monkeypatch)
    v = _verdict_from_gold()
    plan_review._apply_completion_floor_to_verdict(
        v, _gold_map(), floor=_FLOOR, preserve=_PRESERVE, delivered_ids=_delivered_ids()
    )

    dropped_ids = {f["id"] for f in v["dropped"]}
    expected_drop = {c.id for c in GOLD_SET if c.expect_drop}
    assert dropped_ids == expected_drop
    # every anchor category (everything but DROP) is fully preserved
    for c in GOLD_SET:
        if c.category != "DROP":
            assert c.id not in dropped_ids, f"{c.category} anchor {c.id} was wrongly dropped"
    # and the only category dropped is DROP
    assert {c.category for c in GOLD_SET if c.id in dropped_ids} == {"DROP"}


def test_floor_inactive_is_byte_identical(monkeypatch) -> None:
    """With the completion floor gate OFF, a container verdict is untouched (the back-out)."""
    import types

    from rebar import config as core_config

    _partial_epic(monkeypatch)
    monkeypatch.setattr(
        core_config,
        "load_config",
        lambda repo_root=None: types.SimpleNamespace(
            verify=types.SimpleNamespace(
                completion_floor_active=False,
                completion_priority_floor=_FLOOR,
                completion_preserve_criteria=("T5c", "T10"),
            )
        ),
    )
    v = _verdict_from_gold()
    before = {"advisory": [dict(f) for f in v["advisory"]], "coverage": dict(v["coverage"])}
    ctx = types.SimpleNamespace(plan_text="PLAN", has_children=True)
    plan_review._maybe_apply_completion_floor(
        "epic", v, ctx=ctx, cfg=object(), runner=object(), repo_root=None
    )
    assert [f["id"] for f in v["advisory"]] == [f["id"] for f in before["advisory"]]
    assert v["dropped"] == []
    assert "narrowed" not in v["coverage"]


# ── the reopen auto-resurface invariant (recompute every run; no persisted drop) ────────────────
def test_reopen_resurfaces_dropped_finding(monkeypatch) -> None:
    drop_case = next(c for c in GOLD_SET if c.category == "DROP")  # e.g. del-a
    findings = [{"id": drop_case.id, "priority": 0.1, "criteria": drop_case.finding["criteria"]}]
    cmap = {0: drop_case.gold}

    # round 1: del-a delivered → its plan-semantics finding is dropped
    _partial_epic(monkeypatch, delivered=DELIVERED_CHILD_IDS)
    v1 = {"advisory": list(findings), "dropped": [], "coverage": {"counts": {}}}
    plan_review._apply_completion_floor_to_verdict(
        v1, cmap, floor=_FLOOR, preserve=_PRESERVE, delivered_ids=_delivered_ids()
    )
    assert [f["id"] for f in v1["dropped"]] == [drop_case.id]

    # round 2: del-a REOPENED → delivered_now recomputes False → excluded from the manifest →
    # not in delivered_ids → the SAME finding resurfaces (no persisted suppression).
    reopened = DELIVERED_CHILD_IDS - {drop_case.gold["attribution"]}
    _partial_epic(monkeypatch, delivered=reopened)
    v2 = {"advisory": list(findings), "dropped": [], "coverage": {"counts": {}}}
    plan_review._apply_completion_floor_to_verdict(
        v2, cmap, floor=_FLOOR, preserve=_PRESERVE, delivered_ids=_delivered_ids()
    )
    assert v2["dropped"] == []
    assert [f["id"] for f in v2["advisory"]] == [drop_case.id]


# ── non-regression: only what a RE-FIRED REVIEW does changes ────────────────────────────────────
def test_floor_is_a_pure_review_only_transform(monkeypatch) -> None:
    """The floor only RE-PARTITIONS findings between advisory/dropped: it fabricates or loses
    no finding, emits no signing/attestation side-output, and does not mutate its inputs. This
    is the behavioral replacement for the old inspect.getsource token-absence check — a pure
    verdict→verdict transform has no store/LLM pathway to observe, so its review-only nature is
    proven by its OBSERVABLE purity contract (conservation + no side-output + inputs unchanged)."""
    import copy

    _partial_epic(monkeypatch)
    v = _verdict_from_gold()
    cmap = _gold_map()

    before_ids = {f["id"] for f in v["advisory"]} | {f["id"] for f in v.get("dropped", [])}
    before_keys = set(v.keys())
    cmap_before = copy.deepcopy(cmap)
    findings_before = {f["id"]: copy.deepcopy(f) for f in v["advisory"]}

    plan_review._apply_completion_floor_to_verdict(
        v, cmap, floor=_FLOOR, preserve=_PRESERVE, delivered_ids=_delivered_ids()
    )

    # 1. Findings conserved — nothing fabricated or silently lost (the observable analogue of
    #    "never signs/transitions/reopens": a floor that did more than re-partition fails here).
    after_ids = {f["id"] for f in v["advisory"]} | {f["id"] for f in v["dropped"]}
    assert after_ids == before_ids, "the floor fabricated or lost a finding"
    assert v["dropped"], "precondition: this fixture drops at least one finding (non-vacuous)"

    # 2. No signing/attestation side-output: the only NEW top-level verdict key is `dropped`
    #    (a coverage narrowing lives UNDER `coverage`, not as a new top-level key).
    new_keys = set(v.keys()) - before_keys
    assert new_keys <= {"dropped"}, f"floor emitted unexpected side-output keys: {new_keys}"
    for forbidden in ("signature", "attestation", "attestations"):
        assert forbidden not in v, f"the floor emitted a {forbidden!r} key — not review-only"

    # 3. Inputs not mutated: the classification map is unchanged, and a finding the floor KEEPS
    #    in advisory is byte-identical (a dropped finding may legitimately gain a drop annotation,
    #    but a surviving one must not be rewritten).
    assert cmap == cmap_before, "the floor mutated its cmap input"
    for f in v["advisory"]:
        if f["id"] in findings_before:
            assert f == findings_before[f["id"]], f"the floor rewrote kept finding {f['id']}"

    # 4. No NEW attestation kind: the plan-review manifest prefix is unchanged (artifact contract).
    assert attest._MANIFEST_PREFIX == "plan-review"


# ── anti-laundering: a supersede edge cannot launder a security/contract finding into a drop ─────
# Story 457a AC: "the security/contract preserve-set (T5c/T10) is never dropped regardless of
# supersede — so a manipulated edge cannot launder a security finding." A force-closed (unverified)
# child B can be made `delivered-now` purely by adding a live sibling A that supersedes it (branch B
# of delivered_now); this test proves the floor's preserve-set veto beats that path. It uses the
# REAL delivered_now (only its store/attestation reads are mocked), so the supersede branch is
# genuinely exercised, not stubbed.
def _supersede_delivered_epic(monkeypatch) -> None:
    """B is force-closed + UNSIGNED, but a LIVE sibling A supersedes it — so B is delivered-now
    ONLY through the supersede edge (the laundering vector)."""
    b = {"ticket_id": "launder-B", "status": "closed", "parent_id": "epic", "description": _AC}
    a = {
        "ticket_id": "super-A",
        "status": "in_progress",
        "parent_id": "epic",
        "description": _AC,
        "deps": [{"relation": "supersedes", "target_id": "launder-B", "link_uuid": "u1"}],
    }
    children = [b, a]
    monkeypatch.setattr("rebar._reads.list_tickets", lambda *, parent, repo_root=None: children)
    monkeypatch.setattr(
        "rebar._reads.show_ticket",
        lambda cid, repo_root=None: next(c for c in children if c["ticket_id"] == cid),
    )
    # nothing is attested → B is delivered ONLY via the supersede edge (the real delivered_now path)
    monkeypatch.setattr(
        rebar, "verify_signature", lambda tid, *, kind=None, repo_root=None: {"verdict": "unsigned"}
    )
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda tid, repo_root=None: "m1")


def test_supersede_edge_cannot_launder_security_or_contract_finding(monkeypatch) -> None:
    _supersede_delivered_epic(monkeypatch)
    # the REAL delivered_now supersede branch delivers B (only via the edge); A stays in scope
    delivered = _delivered_ids()
    assert delivered == {"launder-B"}, delivered

    # three findings on the supersede-delivered child B, all classified as delivered plan-semantics
    # below the floor: T5c (security) + T10 (contract) MUST be kept; a non-preserve COH IS dropped
    # (proving the supersede delivery is real — so the preserve veto is what stops the laundering).
    v = {
        "advisory": [
            {"id": "sec", "priority": 0.1, "criteria": ["T5c"]},
            {"id": "contract", "priority": 0.1, "criteria": ["T10"]},
            {"id": "plain", "priority": 0.1, "criteria": ["COH"]},
        ],
        "dropped": [],
        "coverage": {"counts": {}},
    }
    ans = {
        "attribution": "launder-B",
        "containment": "limited-to-closed",
        "layer": "plan-semantics",
    }
    plan_review._apply_completion_floor_to_verdict(
        v, {0: ans, 1: ans, 2: ans}, floor=_FLOOR, preserve=_PRESERVE, delivered_ids=delivered
    )
    kept = {f["id"] for f in v["advisory"]}
    dropped = {f["id"] for f in v["dropped"]}
    assert "sec" in kept and "contract" in kept  # preserve veto beats the supersede-laundering path
    assert dropped == {"plain"}  # the non-preserve finding IS dropped → the edge really did deliver
