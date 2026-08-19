"""DIAGNOSIS repro: does an inbound Jira issuelink actually LAND as a local dep? (ticket 5200)

The live cell ``test_inbound_link_round_trips`` reports "the inbound Jira link did not surface as
a local dep". "Did not surface" spans three separable stages, and a count of one failure names
none of them:

  1. the DIFFER — ``inbound_differ._diff_links_inbound`` (defined in the sibling
     ``inbound_collection_diffs.py`` and re-exported) turning a
     Jira ``issuelinks`` entry into ``{"action": "add", "target_id", "relation"}``;
  2. the APPLIER — ``apply_inbound_records._inbound_update_apply_links``
     (``apply_inbound_records.py:527-560``) writing each of those through ``rebar.link``;
  3. the STORE PROJECTION — whether the dep then reads back on the ticket the cell inspects,
     which is the ONE the oracle actually looks at.

This module drives all three against a REAL rebar store in a temp dir (no live Jira), so a red
here localises the fault to a stage instead of to a direction.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest


class _FakeBindingStore:
    """The two lookups the link differ + its bidir suppression use."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._by_key = dict(mapping)
        self._by_local = {v: k for k, v in mapping.items()}

    def get_local_id(self, jira_key: str) -> str | None:
        return self._by_key.get(jira_key)

    def get_jira_key(self, local_id: str) -> str | None:
        return self._by_local.get(local_id)


def _blocks_outward(other_key: str) -> dict[str, Any]:
    """The issuelink Jira shows on X when X BLOCKS ``other_key`` (outward side)."""
    return {
        "id": "10001",
        "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        "outwardIssue": {"key": other_key},
    }


def _blocks_inward(other_key: str) -> dict[str, Any]:
    """The MIRROR entry Jira shows on the far end of the same link (inward side)."""
    return {
        "id": "10001",
        "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        "inwardIssue": {"key": other_key},
    }


@pytest.fixture
def two_tickets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, str]:
    """A real initialised store holding two same-type, parentless tickets."""
    import subprocess

    import rebar

    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.com"),
        ("git", "config", "user.name", "d"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    a = rebar.create_ticket("task", "J11 link source", repo_root=repo)
    b = rebar.create_ticket("task", "J11 link target", repo_root=repo)
    return repo, str(a), str(b)


# ---------------------------------------------------------------------------
# Stage 1 — the differ
# ---------------------------------------------------------------------------


def test_stage1_the_differ_emits_an_add_for_an_outward_blocks_link() -> None:
    """A bound, present counterpart must yield exactly one ``blocks`` add."""
    inbound_differ = importlib.import_module("rebar_reconciler.inbound_differ")

    bindings = _FakeBindingStore({"DC-1": "local-a", "DC-2": "local-b"})
    local_a = {"ticket_id": "local-a", "deps": []}
    local_b = {"ticket_id": "local-b", "deps": []}

    muts = inbound_differ._diff_links_inbound(
        {"issuelinks": [_blocks_outward("DC-2")]},
        local_a,
        bindings,
        {"local-a": local_a, "local-b": local_b},
    )
    assert muts == [{"action": "add", "target_id": "local-b", "relation": "blocks"}], (
        f"the differ did not emit the inbound blocks add; got {muts!r}"
    )


def test_stage1_an_unbound_counterpart_is_skipped_silently() -> None:
    """THE MECHANISM BEHIND THE LIVE FAILURE, variable 1 of 2: the counterpart is UNBOUND.

    Identical to the passing case above except that ``DC-2`` has no binding. On the pass that
    first sees a DC-side link target, that is exactly the state: the inbound differ runs at
    ``run_differs.py:239`` and the binding walk that would adopt ``DC-2`` runs at
    ``run_differs.py:240``, AFTER it — and nothing is applied until ``reconcile.py:454``. So the
    link is dropped at ``inbound_differ.py:402-404`` with no mutation, no alert, and no log.
    """
    inbound_differ = importlib.import_module("rebar_reconciler.inbound_differ")

    bindings = _FakeBindingStore({"DC-1": "local-a"})  # DC-2 deliberately unbound
    local_a = {"ticket_id": "local-a", "deps": []}

    muts = inbound_differ._diff_links_inbound(
        {"issuelinks": [_blocks_outward("DC-2")]},
        local_a,
        bindings,
        {"local-a": local_a},
    )
    assert muts == [], (
        f"an unbound counterpart must be skipped for a later pass, not emitted; got {muts!r}"
    )


def test_stage1_a_bound_but_locally_absent_counterpart_is_skipped_silently() -> None:
    """THE MECHANISM, variable 2 of 2: the counterpart is BOUND but not in the active local set.

    ``local_tickets`` is read ONCE at pass start (``reconcile.py:250``), so a local ticket the
    same pass is about to CREATE for ``DC-2`` is not in it. The dormant-counterpart guard at
    ``inbound_differ.py:409-412`` then drops the link. Varying only the local-set membership
    from the passing case isolates this from the unbound case above.
    """
    inbound_differ = importlib.import_module("rebar_reconciler.inbound_differ")

    bindings = _FakeBindingStore({"DC-1": "local-a", "DC-2": "local-b"})
    local_a = {"ticket_id": "local-a", "deps": []}

    muts = inbound_differ._diff_links_inbound(
        {"issuelinks": [_blocks_outward("DC-2")]},
        local_a,
        bindings,
        {"local-a": local_a},  # local-b absent from the active set
    )
    assert muts == [], (
        f"a counterpart missing from the active local set must be skipped; got {muts!r}"
    )


def test_stage1_the_mirror_entry_on_the_far_end_emits_the_inverse() -> None:
    """The SAME Jira link seen from the far end emits ``depends_on`` back.

    Both endpoints are in the snapshot on a real pass, so the differ visits both. Recording
    this explicitly because it is what makes the applier write TWO edges for one Jira link.
    """
    inbound_differ = importlib.import_module("rebar_reconciler.inbound_differ")

    bindings = _FakeBindingStore({"DC-1": "local-a", "DC-2": "local-b"})
    local_a = {"ticket_id": "local-a", "deps": []}
    local_b = {"ticket_id": "local-b", "deps": []}

    muts = inbound_differ._diff_links_inbound(
        {"issuelinks": [_blocks_inward("DC-1")]},
        local_b,
        bindings,
        {"local-a": local_a, "local-b": local_b},
    )
    assert muts == [{"action": "add", "target_id": "local-a", "relation": "depends_on"}], (
        f"the far-end mirror did not emit the inverse relation; got {muts!r}"
    )


# ---------------------------------------------------------------------------
# Stage 2 + 3 — the applier and the projection the live oracle reads
# ---------------------------------------------------------------------------


def test_stage2_the_applier_writes_the_dep_onto_the_source_ticket(
    two_tickets: tuple[Path, str, str],
) -> None:
    """THE ORACLE'S OWN READ. After applying the differ's add, does ``a.deps`` name ``b``?

    ``test_inbound_link_round_trips`` reads ``deps[*].target_id`` off the ticket the DC-side
    link was created FROM, so that exact projection is what is asserted here.
    """
    import rebar

    repo, a, b = two_tickets
    apply_records = importlib.import_module("rebar_reconciler.apply_inbound_records")

    applied = apply_records._inbound_update_apply_links(
        {"links": [{"action": "add", "target_id": b, "relation": "blocks"}]},
        a,
        repo,
    )
    assert applied == 1, f"the applier reported {applied} links applied, expected 1"

    deps = rebar.show_ticket(a, repo_root=repo).get("deps") or []
    targets = {d.get("target_id") for d in deps}
    assert b in targets, f"the applied inbound link did not surface on {a}: deps={json.dumps(deps)}"


def test_stage3_applying_both_directions_of_one_jira_link_is_survivable(
    two_tickets: tuple[Path, str, str],
) -> None:
    """A real pass applies BOTH mirror entries. The first edge must survive the second.

    ``rebar.link`` owns cycle detection and the redundant-link guard; ``blocks(a,b)`` followed
    by ``depends_on(b,a)`` describes the SAME blocking edge, so the second write is either a
    no-op or a rejection. Either is fine — what must NOT happen is the first edge vanishing,
    because that is indistinguishable at the oracle from "the link never arrived".
    """
    import rebar

    repo, a, b = two_tickets
    apply_records = importlib.import_module("rebar_reconciler.apply_inbound_records")

    apply_records._inbound_update_apply_links(
        {"links": [{"action": "add", "target_id": b, "relation": "blocks"}]}, a, repo
    )
    apply_records._inbound_update_apply_links(
        {"links": [{"action": "add", "target_id": a, "relation": "depends_on"}]}, b, repo
    )

    a_targets = {
        d.get("target_id") for d in (rebar.show_ticket(a, repo_root=repo).get("deps") or [])
    }
    b_targets = {
        d.get("target_id") for d in (rebar.show_ticket(b, repo_root=repo).get("deps") or [])
    }
    assert b in a_targets or a in b_targets, (
        f"after applying both mirror entries NEITHER endpoint carries the edge: "
        f"{a}.deps->{sorted(a_targets)}, {b}.deps->{sorted(b_targets)}"
    )
    assert b in a_targets, (
        f"the FIRST-written edge was lost when the mirror was applied: {a}.deps->"
        f"{sorted(a_targets)} (the live oracle reads exactly this side)"
    )
