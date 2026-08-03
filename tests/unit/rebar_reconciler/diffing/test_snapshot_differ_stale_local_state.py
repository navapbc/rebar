"""Bugs 727f and d103: the diff phase must not act on ``local_state`` emissions when its
``local_state`` argument is not local state.

THE ONE MECHANISM BOTH TICKETS SHARE. ``differ.compute_mutations``'s contract is
``(local_state, jira_state)`` — the LOCAL source of truth against the Jira working set —
and ``differ.py``'s module docstring says so precisely because that contract REPLACED the
legacy ``(prev_snapshot, next_snapshot)`` one. Its sole production caller,
``run_differs``, was never migrated: ``reconcile.py`` builds ``prev_snapshot`` from the
persisted earlier FETCH and ``curr_snapshot`` from a fresh FETCH, so BOTH arguments are
remote Jira state and neither reads ``local_tickets``. At that call site the differ's three
per-key arms divide cleanly:

* key in ``local_state`` only -> outbound create (``unbound_local``). Here that means "was
  in the previous fetch, gone from this one" — deleted, or merely aged out of the fetch
  window. Emitting a create RESURRECTS it. WRONG (ticket d103).
* key in ``jira_state`` only -> inbound create (``jira_new``). Here that means a genuinely
  new remote issue. CORRECT, and the snapshot differ's one real job at this call site.
* key in both -> outbound update (``field_drift``) carrying the STALE prev value. Because
  ``reconcile.py`` advances ``prev_snapshot`` from the PRE-APPLY fetch, an outbound write
  applied during pass N is invisible to ``prev`` at pass N+1, so a fully converged pair is
  re-planned as outbound work — and a read-only pass, which never advances ``prev``,
  re-plans it forever. WRONG (ticket 727f).

WHY NEITHER IS MERELY COSMETIC. The ``field_drift`` phantom's payload is a bare field dict
rather than ``{"changed_fields": ...}``, so the applier resolves its fields to ``{}``: it can
never be satisfied, is planned every pass, spends the bootstrap mutation cap, and makes a
"converged" report untrue. The ``unbound_local`` create is worse — it reaches
``client.create_issue`` and resurrects the issue, which ADR 0028 Decision para 1 forbids
("No destructive or terminal action ... may be driven by a key's absence from the fetched
snapshot").

The LABEL case of the ``field_drift`` echo was already fixed (ticket robe-creek-zealot, the
bridge-internal-label branch in ``_compute_mutations_emit_both``); every other field was left
behind. These tests close the remaining fields and the create arm.

NOTE FOR A FUTURE MIGRATION. The suppression under test lives at the CALL SITE, not in the
differ, precisely so ``compute_mutations``'s documented local-vs-jira contract stays intact for
callers that honour it. If ``run_differs`` is ever migrated to pass real local state, the
suppression must be REMOVED in the same change — at that point both emissions become correct.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"

if "rebar_reconciler" not in sys.modules:  # pragma: no cover - import bootstrap
    _pkg = types.ModuleType("rebar_reconciler")
    _pkg.__path__ = [str(RECON_DIR)]
    sys.modules["rebar_reconciler"] = _pkg


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, RECON_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def run_differs_mod():
    return _load("run_differs_stale_local_state_test", "run_differs.py")


@pytest.fixture(scope="module")
def differ_mod():
    return _load("differ_stale_local_state_test", "differ.py")


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
    "_run_differs_inbound_probe_dispatch",
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
    run_differs_mod.run_differs(ctx, lambda *a, **k: None)
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
    """prev = the pre-apply fetch; curr = the same issue carrying the title WE wrote.

    The pair is converged — the remote already holds the value the reconciler intended —
    so the diff phase must plan nothing outbound for it.
    """
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
    assert _of_kind(raw, "outbound", "update")[0].payload.get("summary") == _OLD, (
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
    """A key in ``prev`` but not ``curr`` must NOT be planned as an outbound create.

    Absence from the current fetch means the issue was deleted OR merely aged out of the
    working-set query (a ``status = Done`` issue beyond the recent cap). Neither licenses a
    create: ADR 0028 Decision para 1 forbids any terminal action driven by snapshot absence,
    and a create targeted at the issue's own Jira key RESURRECTS it from the stale prev
    fields. Deletion is proven only by a bounded direct GET returning 404 (ADR 0028 para 2),
    which is a different subsystem entirely.
    """
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
    """The edge-triggered inbound-create path must survive the suppression.

    A key present in ``curr`` but not ``prev`` is a Jira issue that appeared since the
    last pass; planning its ``(inbound, create)`` is the snapshot differ's real job and
    is what the multi-page pagination coverage depends on.
    """
    prev: dict = {}
    curr = {"RB-9": _issue("a brand new remote issue")}

    planned = _drive_diff_phase(run_differs_mod, differ_mod, monkeypatch, prev, curr)

    inbound_creates = [m for m in _of_kind(planned, "inbound", "create") if m.target == "RB-9"]
    assert inbound_creates, (
        f"the inbound create for a brand-new remote key was lost; planned: "
        f"{[(m.direction, m.action, m.target) for m in planned]}"
    )


def test_seed_mutations_survive_the_suppression(run_differs_mod, differ_mod, monkeypatch):
    """Invariant-seeded mutations are injected by the caller, not derived from the
    mis-shaped ``local_state``, so the suppression must never touch them.

    ``invariants.check_dual_identity_complete`` seeds repair mutations that the differ
    cannot derive from state alone; dropping one would silently skip a repair. The seed
    below deliberately carries the same ``reason`` string as the 727f phantom, so a
    suppression keyed on the reason ALONE — rather than on the differ's own ``source`` —
    would eat it.
    """
    mut_mod = _load("mutation_stale_local_state_test", "mutation.py")
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
