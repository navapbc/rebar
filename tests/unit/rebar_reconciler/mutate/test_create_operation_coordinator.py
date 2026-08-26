"""Held-out behavioral oracle for REB-3115 S4 T3 (2863-c335) — full CREATE coordination.

Pins the OBSERVABLE contract of the S4 T3 cutover:

``rebar_reconciler.create_route``
    ``create_route()`` — the SINGLE rollback selector (default coordinator, one legacy
    value, no dual-send);
    ``coordinate_full_create(...)`` — the pure-decision composition of
    ``coordinate_create`` then ``contain_created`` that ALSO derives the
    create-before-link / parent-before-child gate (``dependents_released``); and
    ``should_hold_dependent(outcome)`` — the dependent gate predicate.

The crash-restart convergence oracle drives the FULL coordinated create across FRESH
"process" restarts with a fault injected at each write-ahead cut point, over BOTH a
Cloud- and a DC-flavored stateful provider, and asserts EXACTLY ONE physical remote
create survives and EXACTLY ONE confirmed forward+reverse binding results (AC1) — with
ZERO search on the keyed-recovery cuts (AC2). A known-key post-create failure aborts to
``safety_aborted`` with the key preserved and NO delete seam ever invoked. Assertions
are OBSERVABLE ONLY (enums / buckets / counts / keys / gate verdicts).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

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
def route_mod():
    return _load("create_route_t3_test", "create_route.py")


@pytest.fixture(scope="module")
def batch_mod():
    return _load("batch_dispatch_t3_test", "batch_dispatch.py")


@pytest.fixture(scope="module")
def applier_mod():
    return _load("applier_t3_test", "applier.py")


class _Plan:
    def __init__(self, identity: str) -> None:
        self.identity = identity


# ── A persistent (restart-surviving) store + provider ─────────────────────────────


class _Store:
    """A minimal write-ahead binding store surviving a simulated restart.

    Records forward (local_id -> {state, jira_key}) and reverse (jira_key -> local_id)
    bindings. Never deletes anything on a post-create failure — the key is preserved on
    a keyed-pending entry exactly as the real ``binding_store`` does (bug 387d)."""

    def __init__(self) -> None:
        self.bindings: dict = {}
        self.reverse: dict = {}

    def bind_pending(self, local_id: str) -> None:
        self.bindings.setdefault(local_id, {"state": "pending", "jira_key": None})

    def record_pending_key(self, local_id: str, jira_key: str) -> None:
        self.bindings[local_id] = {"state": "pending", "jira_key": jira_key}

    def bind_confirm(self, local_id: str, jira_key: str) -> None:
        self.bindings[local_id] = {"state": "confirmed", "jira_key": jira_key}
        self.reverse[jira_key] = local_id

    def keyed_pending_key(self, local_id: str) -> str | None:
        entry = self.bindings.get(local_id)
        if entry and entry["state"] == "pending":
            return entry["jira_key"]
        return None

    def confirmed(self) -> list:
        return [(k, v["jira_key"]) for k, v in self.bindings.items() if v["state"] == "confirmed"]


class _Provider:
    """A venue-flavored stateful remote. Indexes created issues by the durable
    idempotency key (``local_id``) so a restart converges to ONE issue. EXPLODES if any
    delete/undo is ever requested (AC6 no-delete, enforced from the provider side)."""

    def __init__(self, venue: str) -> None:
        self.venue = venue
        self.issues: dict = {}  # local_id -> {"key", "labels": set, "props": dict}
        self.physical_create_count = 0
        self.search_count = 0
        self.delete_count = 0
        self._seq = 0

    def create(self, local_id: str) -> str:
        if local_id in self.issues:
            return self.issues[local_id]["key"]
        self._seq += 1
        self.physical_create_count += 1
        key = f"{self.venue.upper()}-{self._seq}"
        self.issues[local_id] = {"key": key, "labels": set(), "props": {}}
        return key

    def register_landed(self, local_id: str) -> str:
        """Model an issue that DID land remotely even though the ack was lost."""
        return self.create(local_id)

    def search(self, local_id: str) -> str | None:
        self.search_count += 1
        issue = self.issues.get(local_id)
        return issue["key"] if issue else None

    def delete(self, key: str) -> None:  # pragma: no cover - must never be called
        self.delete_count += 1
        raise AssertionError(f"{self.venue}: delete must never be issued for {key!r}")


def _seams(route_mod, store, provider, local_id, *, fault_stage=None, ambiguous=None):
    """Build the eight coordinated-create seams over ``store``/``provider``.

    ``fault_stage`` raises once in the named containment seam (record_key / attach_label
    / set_property / confirm). ``ambiguous`` (``proven`` / ``inconclusive``) makes the
    create return a timeout, modeling a landed-but-unacked (proven) or truly-absent
    (inconclusive) issue that the replay-safe observe re-checks."""
    plan = _Plan(local_id)
    CreateSignal = route_mod.CreateSignal
    ObservationSignal = route_mod.ObservationSignal

    def persist_pending(_p):
        store.bind_pending(local_id)

    def create_execute(_p):
        # Recovery: a keyed-pending binding OR an already-created remote issue means the
        # create landed on a prior pass — NEVER issue a second physical create.
        existing = store.keyed_pending_key(local_id) or (
            provider.issues[local_id]["key"] if local_id in provider.issues else None
        )
        if existing is not None:
            return CreateSignal(status="created", known_key=existing)
        if ambiguous is not None:
            if ambiguous == "proven":
                provider.register_landed(local_id)  # landed, ack lost
            return CreateSignal(status="timeout")
        return CreateSignal(status="created", known_key=provider.create(local_id))

    def observe(_p):
        key = provider.search(local_id)
        if key is not None:
            return ObservationSignal(status="proven", known_key=key)
        return ObservationSignal(status="inconclusive")

    def _maybe_fault(stage):
        if fault_stage == stage:
            raise RuntimeError(f"injected fault at {stage}")

    def record_key(_p, known_key):
        _maybe_fault("record_key")
        store.record_pending_key(local_id, known_key)

    def attach_label(_p, known_key):
        _maybe_fault("attach_label")
        provider.issues.setdefault(local_id, {"key": known_key, "labels": set(), "props": {}})
        provider.issues[local_id]["labels"].add(f"rebar-id:{local_id}")

    def set_property(_p, known_key):
        _maybe_fault("set_property")
        provider.issues[local_id]["props"]["local_id"] = local_id

    def confirm(_p, known_key):
        _maybe_fault("confirm")
        store.bind_confirm(local_id, known_key)

    return plan, {
        "persist_pending": persist_pending,
        "create_execute": create_execute,
        "observe": observe,
        "record_key": record_key,
        "attach_label": attach_label,
        "set_property": set_property,
        "confirm": confirm,
    }


# ════════════════════════════════════════════════════════════════════════════════
# AC1 — crash-restart convergence: exactly one create, one confirmed binding.
# ════════════════════════════════════════════════════════════════════════════════

# The write-ahead cut points that must converge to exactly one issue on restart. Each is
# (fault_stage, ambiguous, keyed) — ``keyed`` marks the cuts where restart recovers via a
# keyed-pending binding and therefore performs ZERO search (AC2).
_CUT_POINTS = [
    ("record_key", None, False),
    ("attach_label", None, True),
    ("set_property", None, True),
    ("confirm", None, True),
    (None, "proven", False),
    (None, "inconclusive", False),
]


@pytest.mark.parametrize("venue", ["cloud", "dc"])
@pytest.mark.parametrize(("fault_stage", "ambiguous", "keyed"), _CUT_POINTS)
def test_crash_restart_converges_to_one_issue(route_mod, venue, fault_stage, ambiguous, keyed):
    """AC1: a fault at any write-ahead cut point, replayed on a FRESH restart, converges
    to EXACTLY ONE remote issue and ONE confirmed forward+reverse binding — never a
    duplicate, never a delete. AC2: keyed-recovery cuts perform ZERO search."""
    local_id = f"L-{venue}-{fault_stage}-{ambiguous}"
    store = _Store()  # durable across the restart
    provider = _Provider(venue)  # durable remote

    # ── Pass 1 (crashes at the injected cut point) ──
    plan1, seams1 = _seams(
        route_mod, store, provider, local_id, fault_stage=fault_stage, ambiguous=ambiguous
    )
    out1 = route_mod.coordinate_full_create(plan1, **seams1)
    assert provider.delete_count == 0
    if ambiguous is None:
        # A containment cut aborts to safety_aborted, key preserved, dependents HELD.
        assert out1.disposition.value == "safety_aborted"
        assert out1.dependents_released is False
        assert out1.known_key is not None

    # ── Pass 2 (fresh process, no fault) — must NOT duplicate ──
    plan2, seams2 = _seams(route_mod, store, provider, local_id)
    search_before = provider.search_count
    out2 = route_mod.coordinate_full_create(plan2, **seams2)

    # Exactly ONE physical remote create across BOTH passes.
    assert provider.physical_create_count == 1
    assert provider.delete_count == 0
    # Exactly ONE confirmed forward+reverse binding.
    confirmed = store.confirmed()
    assert len(confirmed) == 1
    (bound_local, bound_key) = confirmed[0]
    assert bound_local == local_id
    assert store.reverse == {bound_key: local_id}
    assert provider.issues[local_id]["key"] == bound_key
    # Convergence releases dependents.
    assert out2.confirmed is True
    assert out2.dependents_released is True
    assert out2.disposition.value in {"applied", "recovered"}
    # AC2: keyed recovery performs ZERO search on the restart pass.
    if keyed:
        assert provider.search_count == search_before, "keyed recovery must not search"


# ════════════════════════════════════════════════════════════════════════════════
# AC5 — dependency gating: links/children wait for proven create + confirmation.
# ════════════════════════════════════════════════════════════════════════════════


def _run(route_mod, store, provider, local_id, **kw):
    plan, seams = _seams(route_mod, store, provider, local_id, **kw)
    return route_mod.coordinate_full_create(plan, **seams)


def test_dependent_held_until_create_and_confirmation_proven(route_mod):
    """AC5: create-before-link / parent-before-child — a dependent is HELD until BOTH
    the create AND its confirmation are proven, released only then."""
    # Confirmed create → dependent released.
    out_ok = _run(route_mod, _Store(), _Provider("cloud"), "P-1")
    assert out_ok.dependents_released is True
    assert route_mod.should_hold_dependent(out_ok) is False

    # Containment safety-abort (confirmation NOT proven) → dependent HELD.
    out_abort = _run(route_mod, _Store(), _Provider("cloud"), "P-2", fault_stage="confirm")
    assert out_abort.dependents_released is False
    assert route_mod.should_hold_dependent(out_abort) is True

    # Create never demonstrably landed (commit_unknown) → dependent HELD.
    out_unknown = _run(route_mod, _Store(), _Provider("cloud"), "P-3", ambiguous="inconclusive")
    assert out_unknown.known_key is None
    assert out_unknown.dependents_released is False
    assert route_mod.should_hold_dependent(out_unknown) is True


# ════════════════════════════════════════════════════════════════════════════════
# AC6 — one coordinator route, one legacy value, no dual-send.
# ════════════════════════════════════════════════════════════════════════════════


def test_create_route_selector_default_and_rollback(route_mod, monkeypatch):
    """AC6: default is coordinator; a falsey env rolls back to the ONE legacy value; an
    invalid value raises."""
    monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    assert route_mod.create_route() == route_mod.COORDINATOR_ROUTE
    for falsey in ("legacy", "0", "false", "off", "no"):
        monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", falsey)
        assert route_mod.create_route() == route_mod.LEGACY_ROUTE
    for truthy in ("coordinator", "1", "true", "on", "yes"):
        monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", truthy)
        assert route_mod.create_route() == route_mod.COORDINATOR_ROUTE
    monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", "banana")
    with pytest.raises(ValueError):
        route_mod.create_route()


def test_route_for_create_tracks_the_single_selector(route_mod, batch_mod, monkeypatch):
    """AC6: ``batch_dispatch.route_for('create')`` is decided by the ONE selector, and
    an ``overrides`` map never applies to create."""
    monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    assert batch_mod.route_for("create") == "coordinator"
    assert batch_mod.route_for("create", {"create": "legacy"}) == "coordinator"
    monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", "legacy")
    assert batch_mod.route_for("create") == "legacy"


def test_no_dual_send_exactly_one_path_per_create(applier_mod, monkeypatch):
    """AC6: a single create runs EXACTLY ONE path — the coordinated write-ahead (default,
    never deletes) OR the legacy create+delete-rollback — never both."""
    mut_mod = applier_mod._load_mutation_module()

    def _mk():
        return mut_mod.Mutation(
            direction=mut_mod.MutationDirection.outbound,
            action=mut_mod.MutationAction.create,
            target="LOCAL-X",
            payload={"summary": "t", "key_hint": "K-1", "local_id": "LOCAL-X"},
            provenance={"source": "t3"},
        )

    from unittest.mock import MagicMock

    # Coordinator default: create succeeds → containment → NO delete, ever.
    monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    c = MagicMock()
    c.create_issue.return_value = {"key": "K-1"}
    res = applier_mod._apply_outbound_create(_mk(), client=c)
    assert c.create_issue.call_count == 1
    c.delete_issue.assert_not_called()
    assert res.payload["coordinated"] is True

    # Coordinator default, create FAILS → still NO delete (bug 387d), dependents held.
    cf = MagicMock()
    cf.create_issue.side_effect = RuntimeError("boom")
    res_f = applier_mod._apply_outbound_create(_mk(), client=cf)
    cf.delete_issue.assert_not_called()
    assert res_f.payload["dependents_released"] is False

    # Legacy toggle: create FAILS → delete-rollback runs and the error re-raises. The
    # coordinated composition is NOT taken (no coordinated payload) — no dual-send.
    monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", "legacy")
    cl = MagicMock()
    cl.create_issue.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        applier_mod._apply_outbound_create(_mk(), client=cl)
    cl.delete_issue.assert_called_once()


# ════════════════════════════════════════════════════════════════════════════════
# Known-key safety abort — key preserved, NO delete, property failure keeps label.
# ════════════════════════════════════════════════════════════════════════════════


def test_known_key_post_create_failure_aborts_without_delete(route_mod):
    """A post-create write failure yields ``safety_aborted`` with the key preserved and
    NO delete seam (there is none). A ``set_property`` failure keeps the label."""
    store = _Store()
    provider = _Provider("cloud")
    out = _run(route_mod, store, provider, "S-1", fault_stage="set_property")
    assert out.disposition.value == "safety_aborted"
    assert out.bucket == "deferred"
    assert out.known_key is not None
    assert out.label_attached is True  # set_property failure keeps the already-attached label
    assert out.property_attached is False
    assert out.confirmed is False
    assert provider.delete_count == 0
    # The key is preserved on a keyed-pending binding for deterministic recovery.
    assert store.keyed_pending_key("S-1") == out.known_key
