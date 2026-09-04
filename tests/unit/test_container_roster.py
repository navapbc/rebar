"""The container pass's sibling roster (bug creamy-cocksure-elkhound).

``G3`` asks whether the union of children covers each parent acceptance criterion, and the
container prompt tells it to flag an absence "only if NO sibling in the roster covers it".
The roster it was handed carried ids and TITLES ONLY, which cannot discharge that burden — so
the anti-false-positive rule correctly silenced the finding every time and G3 could never fire
on an uncovered parent criterion.

These tests pin the repair: the roster carries each child's acceptance criteria, ONE shared
builder serves every caller (so the eval harnesses cannot drift from production), the roster
rides the cached prefix instead of being re-sent per pairing, and its tokens are counted in
the prefix estimate that packs container bins.
"""

from __future__ import annotations

import inspect

from rebar.llm.config import LLMConfig
from rebar.llm.evals import eval_solver
from rebar.llm.plan_review import fidelity_spot_eval, pass1, passes, registry

_CHILD_A = {
    "ticket_id": "c1",
    "title": "Detectors",
    "description": (
        "## Scope\n- src/rebar/detect.py\n"
        "## Acceptance Criteria\n"
        "- [ ] the detector rejects an empty payload\n"
        "- [ ] the detector emits a typed error\n"
    ),
}
_CHILD_B = {
    "ticket_id": "c2",
    "title": "Reviewer prompts",
    "description": "## Acceptance Criteria\n- [ ] the prompt names its output schema\n",
}
_CHILD_NO_AC = {
    "ticket_id": "c3",
    "title": "Untriaged follow-up",
    "description": "## Context\nSomething to look at later. No criteria yet.\n",
}


def _fake_cfg() -> LLMConfig:
    return LLMConfig(model="fake-model")


class _Capturing:
    """Captures the RunRequest so we can assert WHERE the roster was sent."""

    name = "capturing"

    def __init__(self) -> None:
        self.requests: list[object] = []

    def preflight(self) -> None:
        pass

    def run(self, req):  # type: ignore[no-untyped-def]
        self.requests.append(req)
        return {"findings": []}


# ── roster content ────────────────────────────────────────────────────────────
def test_roster_carries_child_ac() -> None:
    """The evidence G3's absence test needs: each child's criteria, not just its title."""
    roster = pass1.build_sibling_roster([_CHILD_A, _CHILD_B])
    assert "the detector rejects an empty payload" in roster
    assert "the detector emits a typed error" in roster
    assert "the prompt names its output schema" in roster


def test_roster_render_format() -> None:
    """Each child's criteria are INDENTED beneath its ``- <id>: <title>`` line, which is the
    structure G3's rubric refers to when it attributes coverage to a specific sibling."""
    roster = pass1.build_sibling_roster([_CHILD_A])
    lines = roster.splitlines()
    assert lines[0] == "- c1: Detectors"
    assert all(line.startswith("  ") for line in lines[1:])
    assert "the detector rejects an empty payload" in lines[1]


def test_roster_degrades_gracefully() -> None:
    """A child with no parseable criteria keeps its bare line — the roster degrades to the
    historical title-only shape rather than DROPPING the child, because a dropped child would
    silently look like a sibling that cannot cover anything. An empty roster must not raise."""
    roster = pass1.build_sibling_roster([_CHILD_NO_AC])
    assert roster == "- c3: Untriaged follow-up"

    mixed = pass1.build_sibling_roster([_CHILD_A, _CHILD_NO_AC])
    assert "- c3: Untriaged follow-up" in mixed.splitlines()

    assert pass1.build_sibling_roster([]) == ""


def test_roster_tolerates_absent_description() -> None:
    """A child dict with no description at all (a thin store record) must not raise."""
    assert pass1.build_sibling_roster([{"ticket_id": "c9", "title": "Thin"}]) == "- c9: Thin"


# ── single sourcing ───────────────────────────────────────────────────────────
def test_roster_builder_single_sourced() -> None:
    """Every non-test caller of ``pass1_container`` builds its roster with the ONE shared
    helper. Three independent copies of the expression existed; the two eval paths would have
    kept sending title-only rosters while production sent AC-bearing ones — and those
    harnesses are what generate the dogfooding evidence for G3's deferred posture decision,
    so the divergence would have corrupted the data it rests on.
    """
    for module in (pass1, fidelity_spot_eval, eval_solver):
        src = inspect.getsource(module)
        assert "build_sibling_roster" in src, f"{module.__name__} does not use the shared builder"

    # No caller retains a local copy of the roster-building expression.
    for module in (fidelity_spot_eval, eval_solver):
        src = inspect.getsource(module)
        assert "{c.get('title', '')}" not in src, f"{module.__name__} still builds a roster itself"


def test_eval_fixture_roster_override_wins() -> None:
    """``eval_solver`` lets a fixture pin a roster verbatim; the shared builder is only the
    FALLBACK. Swapping the builder in unconditionally would have broken those fixtures."""
    src = inspect.getsource(eval_solver)
    assert 'case.get("sibling_roster") or build_sibling_roster(' in src


# ── where the roster is sent ──────────────────────────────────────────────────
def test_roster_rides_prefix_not_instructions() -> None:
    """The roster is byte-identical across a review's pairings, so it belongs in the cached
    prefix. Left in ``instructions`` (after the per-pairing children block) it would be
    re-sent once per pairing — cost growing with the SQUARE of the child count now that it
    carries every child's criteria."""
    cap = _Capturing()
    passes.pass1_container(
        cap,
        _fake_cfg(),
        parent_plan="## Acceptance Criteria\n- [ ] the parent outcome holds\n",
        children=[_CHILD_A],
        criteria=[registry.by_id()["G3"]],
        sibling_roster=pass1.build_sibling_roster([_CHILD_A, _CHILD_B]),
    )
    req = cap.requests[0]
    assert "the prompt names its output schema" in req.system_prompt  # type: ignore[attr-defined]
    assert "the prompt names its output schema" not in req.instructions  # type: ignore[attr-defined]


def test_roster_prefix_is_byte_stable_across_pairings() -> None:
    """The cache only pays off if the prefix is identical between pairings — the property
    the warm-then-fan-out gate assumes."""
    cap = _Capturing()
    roster = pass1.build_sibling_roster([_CHILD_A, _CHILD_B])
    for child in (_CHILD_A, _CHILD_B):
        passes.pass1_container(
            cap,
            _fake_cfg(),
            parent_plan="## Acceptance Criteria\n- [ ] the parent outcome holds\n",
            children=[child],
            criteria=[registry.by_id()["G3"]],
            sibling_roster=roster,
        )
    assert cap.requests[0].system_prompt == cap.requests[1].system_prompt  # type: ignore[attr-defined]


def test_roster_counted_in_prefix_estimate() -> None:
    """CORRECTNESS, not just cost: ``parent_tokens`` feeds ``budget.pack_container_bins`` and
    the per-pairing size estimate as well as the warm gate. With the roster in the prefix but
    absent from the estimate, a bin can be packed OVER the window budget."""
    src = inspect.getsource(pass1._run_container)
    assert "est_tokens(roster)" in src
    # And it is added to the parent-plan estimate rather than replacing it.
    assert "est_tokens(ctx.plan_text) + det_floor.est_tokens(roster)" in src


# ── G3's rubric and its untouched posture ─────────────────────────────────────
def test_g3_rubric_cites_roster_ac() -> None:
    """G3 must be TOLD the roster now carries criteria, with a decision rule — otherwise the
    richer roster arrives and the rubric still reasons as if it held only titles."""
    rubric = registry.by_id()["G3"]["scenario"]
    lowered = rubric.lower()
    assert "roster" in lowered
    assert "indent" in lowered
    assert "title alone" in lowered or "not evidence" in lowered


def test_g3_posture_unchanged() -> None:
    """Revisiting G3's posture is explicitly deferred until this change has produced
    dogfooding data, so the repair must not smuggle a posture change in with it."""
    g3 = registry.by_id()["G3"]
    assert g3["default_posture"] == "advisory"
    assert g3["block_threshold"] == 0.95
    assert g3["facet"] == "container"
