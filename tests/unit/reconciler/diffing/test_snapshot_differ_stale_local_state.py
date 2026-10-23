"""The production diff phase compares two remote snapshots, not true local state. Suppress its
stale outbound update and create arms, preserve genuinely new inbound issues and
caller-seeded repairs, and never infer deletion from fetch-window absence."""

from __future__ import annotations

import importlib
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "reconciler"


@pytest.fixture(scope="module")
def run_differs_mod():
    return importlib.import_module("rebar.reconciler.run_differs")


@pytest.fixture(scope="module")
def differ_mod():
    return importlib.import_module("rebar.reconciler.differ")


def _issue(summary: str, *, labels: list[str] | None = None) -> dict:
    """A Jira snapshot entry in the shape the fetcher stores (raw vendor fields)."""
    return {
        "summary": summary,
        "description": None,
        "issuetype": {"id": "10002", "name": "Task"},
        "priority": {"id": "3", "name": "Medium"},
        "status": {"id": "10000", "name": "To Do"},
        "labels": list(labels or []),
        "issuelinks": [],
    }


_OLD = "rebar bound fixture"
_NEW = "rebar converged title"

_STUBBED_PHASES = (
    "_run_differs_report_schema_drift",
    "_run_differs_inbound",
    "_run_differs_binding_walk",
)


def _stub_surrounding_phases(run_differs_mod, monkeypatch, *, seed_mutations=()):
    """Neutralise every phase around the snapshot differ so the assertions below see
    exactly what the snapshot differ contributed and nothing else."""
    monkeypatch.setattr(
        run_differs_mod,
        "_run_differs_invariants",
        lambda ctx: (False, set(), list(seed_mutations)),
    )
    for name in _STUBBED_PHASES:
        monkeypatch.setattr(run_differs_mod, name, lambda *a, **k: None)
    monkeypatch.setattr(run_differs_mod, "_load_reconcile_backend", lambda: None)
    monkeypatch.setattr(run_differs_mod, "_run_differs_outbound", lambda *a, **k: ([], {}, None))


def _drive_diff_phase(run_differs_mod, differ_mod, monkeypatch, prev, curr, *, seed_mutations=()):
    """Run the real diff phase over ``prev``/``curr``, with the phases that follow
    the snapshot differ stubbed out, and return the accumulated mutation list."""
    _stub_surrounding_phases(run_differs_mod, monkeypatch, seed_mutations=seed_mutations)
    ctx = types.SimpleNamespace(
        differ=differ_mod,
        invariants_mod=None,
        prev_snapshot=prev,
        curr_snapshot=curr,
        mutations=[],
    )
    run_differs_mod.run_differs(ctx)
    return ctx.mutations


def _of_kind(mutations, direction: str, action: str) -> list:
    return [
        m
        for m in mutations
        if str(getattr(m.direction, "value", m.direction)) == direction
        and str(getattr(m.action, "value", m.action)) == action
    ]


# ---------------------------------------------------------------------------
# Ticket 727f — the "key in both" arm
# ---------------------------------------------------------------------------


def test_a_converged_pair_plans_no_outbound_update_after_our_own_write(
    run_differs_mod, differ_mod, monkeypatch
):
    """A converged post-write snapshot must not plan another outbound update."""
    # The rebar-id label is the marker the reconciler writes back on inbound create,
    # so a bound pair really does carry it in both snapshots.
    bound = ["rebar-id:jira-rb-1"]
    prev = {"RB-1": _issue(_OLD, labels=bound)}
    curr = {"RB-1": _issue(_NEW, labels=bound)}

    # FIXTURE PRECONDITION: the snapshot differ really does produce the phantom from
    # this pair, so a green result below cannot come from an inert fixture.
    raw = differ_mod.compute_mutations(local_state=prev, jira_state=curr)
    assert _of_kind(raw, "outbound", "update"), (
        "fixture is inert: the snapshot differ emitted no outbound update for a "
        "prev/curr pair that differs on summary, so this test could pass vacuously"
    )
    assert (
        _of_kind(raw, "outbound", "update")[0].payload.get("changed_fields", {}).get("summary")
        == _OLD
    ), (
        "fixture drifted: the phantom no longer carries the STALE prev value, so it is "
        "not the mechanism this test exists to pin"
    )

    planned = _drive_diff_phase(run_differs_mod, differ_mod, monkeypatch, prev, curr)

    assert _of_kind(planned, "outbound", "update") == [], (
        "the diff phase planned an outbound update for a CONVERGED pair. The remote "
        "already holds the value we wrote; this is the phantom echo of our own pass-N "
        f"write, carrying the stale prev value: "
        f"{[(m.target, m.payload) for m in _of_kind(planned, 'outbound', 'update')]}"
    )


# ---------------------------------------------------------------------------
# Ticket d103 — the "key in local_state only" arm
# ---------------------------------------------------------------------------


def test_a_key_that_left_the_fetch_window_plans_no_outbound_create(
    run_differs_mod, differ_mod, monkeypatch
):
    """A key absent from the current fetch window must not be recreated from stale data."""
    bound = ["rebar-id:jira-rb-7"]
    prev = {"RB-7": _issue("an issue that left the window", labels=bound)}
    curr: dict = {}

    # FIXTURE PRECONDITION: the differ really does emit the resurrect-create from this
    # pair, so a green result below cannot come from an inert fixture.
    raw_creates = _of_kind(
        differ_mod.compute_mutations(local_state=prev, jira_state=curr), "outbound", "create"
    )
    assert raw_creates, (
        "fixture is inert: the snapshot differ emitted no outbound create for a key "
        "present in prev and absent from curr, so this test could pass vacuously"
    )
    assert raw_creates[0].provenance.get("reason") == "unbound_local", (
        "fixture drifted: the create no longer comes from the local-only arm, so it is "
        f"not the mechanism this test exists to pin ({raw_creates[0].provenance})"
    )

    planned = _drive_diff_phase(run_differs_mod, differ_mod, monkeypatch, prev, curr)

    assert _of_kind(planned, "outbound", "create") == [], (
        "the diff phase planned an outbound CREATE for a key that merely left the fetch "
        "window. Applied, this resurrects a deleted issue from stale snapshot fields, and "
        "it fires identically for any issue that ages out of the working-set query: "
        f"{[(m.target, m.provenance) for m in _of_kind(planned, 'outbound', 'create')]}"
    )


def test_key_set_prev_emits_no_outbound_creates_for_large_departed_window(differ_mod):
    """Empty membership entries cannot resurrect 900+ keys that leave the fetch window."""
    full_prev = {f"RB-{index}": _issue(f"departed {index}") for index in range(1000)}
    key_set_prev = {key: {} for key in full_prev}

    full_creates = _of_kind(
        differ_mod.compute_mutations(local_state=full_prev, jira_state={}),
        "outbound",
        "create",
    )
    key_set_creates = _of_kind(
        differ_mod.compute_mutations(local_state=key_set_prev, jira_state={}),
        "outbound",
        "create",
    )

    assert len(full_creates) == 1000, "positive control must exercise the local-only arm"
    assert key_set_creates == []


def test_key_set_prev_field_drift_never_survives_the_production_filter(
    run_differs_mod, differ_mod, monkeypatch
):
    """The both-arm may see empty entries, but no field-bearing outbound effect survives."""
    prev = {"RB-8": {}}
    curr = {"RB-8": _issue("current remote fields")}

    raw_updates = _of_kind(
        differ_mod.compute_mutations(local_state=prev, jira_state=curr),
        "outbound",
        "update",
    )
    assert raw_updates, "positive control must exercise the both-sides field-drift arm"
    assert raw_updates[0].payload

    planned = _drive_diff_phase(run_differs_mod, differ_mod, monkeypatch, prev, curr)

    assert _of_kind(planned, "outbound", "update") == []


# ---------------------------------------------------------------------------
# Positive controls — the suppression must not over-filter
# ---------------------------------------------------------------------------


def test_a_genuinely_new_remote_key_still_plans_its_inbound_create(
    run_differs_mod, differ_mod, monkeypatch
):
    """A newly observed remote key must still plan an inbound create."""
    prev: dict = {}
    curr = {"RB-9": _issue("a brand new remote issue")}

    planned = _drive_diff_phase(run_differs_mod, differ_mod, monkeypatch, prev, curr)

    inbound_creates = [m for m in _of_kind(planned, "inbound", "create") if m.target == "RB-9"]
    assert inbound_creates, (
        f"the inbound create for a brand-new remote key was lost; planned: "
        f"{[(m.direction, m.action, m.target) for m in planned]}"
    )


def test_seed_mutations_survive_the_suppression(run_differs_mod, differ_mod, monkeypatch):
    """Caller-seeded repairs must survive even when their reason resembles a stale diff."""
    mut_mod = importlib.import_module("rebar.reconciler.mutation")
    seed = mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.update,
        target="RB-5",
        payload={"changed_fields": {"summary": "seeded repair"}},
        provenance={"source": "invariants", "reason": "field_drift", "local_id": "RB-5"},
    )

    planned = _drive_diff_phase(
        run_differs_mod, differ_mod, monkeypatch, {}, {}, seed_mutations=[seed]
    )

    assert [m.target for m in planned] == ["RB-5"], (
        "an invariant-seeded mutation was dropped by the snapshot-differ suppression; "
        "the suppression must key on the differ's own provenance, not on the reason "
        f"string alone. Planned: {[(m.target, m.provenance) for m in planned]}"
    )
