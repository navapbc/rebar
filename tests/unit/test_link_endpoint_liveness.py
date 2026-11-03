"""Which link endpoints must be open is one table, and a closed ``depends_on`` target is allowed.

``depends_on`` records that work rests on other work; once that work has merged and closed the
dependency is satisfied, not invalid. The link writer used to refuse a closed ``depends_on``
target (a rule inherited from the bash engine with no rationale), so a plan building on merged
work could not record the edge that plan-review ``P6`` asks for. These tests pin every
relation's endpoint rule against ``_links._ENDPOINT_LIVENESS``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands.link_revert import link_cli
from rebar.graph import _links
from rebar.graph._links import CANONICAL_RELATIONS, CyclicDependencyError, add_dependency
from rebar.llm.plan_review.det_advisory import p6_ac_quality
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.reducer import reduce_ticket

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.chdir(repo)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: Path) -> str:
    return str(repo / ".tickets-tracker")


def _state(repo: Path, tid: str) -> dict:
    return reduce_ticket(str(repo / ".tickets-tracker" / tid)) or {}


def _edges(repo: Path, tid: str, relation: str) -> set[str]:
    return {
        str(dep.get("target_id"))
        for dep in _state(repo, tid).get("deps") or []
        if dep.get("relation") == relation
    }


def _open_and_closed(repo: Path) -> tuple[str, str]:
    """An open plan ticket P and a sibling C whose work merged and closed."""
    plan = rebar.create_ticket("task", "P: builds on C", repo_root=str(repo))
    done = rebar.create_ticket("task", "C: delivered and merged", repo_root=str(repo))
    rebar.transition(done, "open", "closed", repo_root=str(repo))
    return plan, done


# --- AC1: depends_on onto a closed target is recorded ----------------------------------------


def test_library_links_depends_on_to_a_closed_target(store):
    plan, done = _open_and_closed(store)

    rebar.link(plan, done, "depends_on", repo_root=str(store))

    assert _edges(store, plan, "depends_on") == {done}
    graph = rebar.deps(plan, repo_root=str(store))
    assert any(
        d.get("target_id") == done and d.get("relation") == "depends_on" for d in graph["deps"]
    )


def test_cli_links_depends_on_to_a_closed_target(store):
    plan, done = _open_and_closed(store)

    assert link_cli([plan, done, "depends_on"]) == 0
    assert _edges(store, plan, "depends_on") == {done}


# --- AC2: a closed depends_on target leaves the source ready -----------------------------------


def test_closed_depends_on_target_keeps_the_source_ready(store):
    plan, done = _open_and_closed(store)
    rebar.link(plan, done, "depends_on", repo_root=str(store))

    ready_ids = {t["ticket_id"] for t in rebar.ready(repo_root=str(store))}

    assert plan in ready_ids
    assert rebar.deps(plan, repo_root=str(store))["ready_to_work"] is True


# --- AC3: P6 credits the edge as a verified upstream prerequisite ------------------------------


def _p6_citation_gaps(repo: Path, plan: str, done: str) -> int:
    ctx = PlanContext(
        ticket_id=plan,
        ticket_type="task",
        title="P: builds on C",
        description=(
            f"## Approach\n\nReuse the tier delivered by [rebar:{done}].\n\n"
            "## Acceptance Criteria\n\n"
            "- [ ] the reuse is covered; proof: `pytest tests/unit/test_x.py -q`\n"
        ),
        state=_state(repo, plan),
        tickets_root=str(repo),
    )
    return int(p6_ac_quality(ctx).coverage["citation_gaps"])


def test_p6_grounds_a_citation_through_a_depends_on_edge_to_a_closed_ticket(store):
    plan, done = _open_and_closed(store)
    assert _p6_citation_gaps(store, plan, done) == 1  # control: no edge yet

    rebar.link(plan, done, "depends_on", repo_root=str(store))

    assert _p6_citation_gaps(store, plan, done) == 0


# --- AC4/AC5: one table states every relation's endpoint rule ---------------------------------


def test_liveness_table_covers_exactly_the_canonical_relations():
    assert set(_links._ENDPOINT_LIVENESS) == set(CANONICAL_RELATIONS)


# relation -> (a closed SOURCE is refused, a closed TARGET is refused)
_EXPECTED_RULES: dict[str, tuple[bool, bool]] = {
    "blocks": (True, False),
    "depends_on": (True, False),
    "relates_to": (True, False),
    "duplicates": (True, False),
    "supersedes": (False, False),
    "discovered_from": (True, False),
    "caused_by": (True, False),
}


def test_expected_rules_name_every_relation():
    assert set(_EXPECTED_RULES) == set(CANONICAL_RELATIONS)


@pytest.mark.parametrize("relation", sorted(CANONICAL_RELATIONS))
def test_each_relation_pins_its_endpoint_rule(relation):
    assert _links._ENDPOINT_LIVENESS[relation] == _EXPECTED_RULES[relation]


@pytest.mark.parametrize("relation", sorted(CANONICAL_RELATIONS))
def test_writer_accepts_a_closed_target_for_every_relation(store, relation):
    plan, done = _open_and_closed(store)

    add_dependency(plan, done, _tracker(store), relation)

    assert _edges(store, plan, relation) == {done}


@pytest.mark.parametrize("relation", sorted(CANONICAL_RELATIONS))
def test_writer_applies_the_closed_source_rule_for_every_relation(store, relation):
    plan, done = _open_and_closed(store)

    if _EXPECTED_RULES[relation][0]:
        with pytest.raises(ValueError, match=f"source ticket '{done}' is closed"):
            add_dependency(done, plan, _tracker(store), relation)
        assert _edges(store, done, relation) == set()
    else:
        add_dependency(done, plan, _tracker(store), relation)
        assert _edges(store, done, relation) == {plan}


def test_an_unlisted_relation_has_no_inherited_rule():
    with pytest.raises(KeyError):
        _links._refuse_closed_endpoints("a", "b", "not_a_relation", "/nonexistent")


# --- edge: a closed target still goes through the cycle guard --------------------------------


def test_closed_depends_on_target_still_runs_the_cycle_guard(store):
    plan = rebar.create_ticket("task", "P", repo_root=str(store))
    done = rebar.create_ticket("task", "C", repo_root=str(store))
    rebar.link(done, plan, "depends_on", repo_root=str(store))
    rebar.transition(done, "open", "closed", repo_root=str(store))

    with pytest.raises(CyclicDependencyError):
        add_dependency(plan, done, _tracker(store), "depends_on")
