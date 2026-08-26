"""HELD-OUT census: REB-3115 S5 T1 — typed route ownership + fallback poison.

After the S3/S4 family cutovers, every supported Cloud/DC mutation must have EXACTLY
ONE typed coordinator/adapter owner, and the obsolete production fallbacks (generic
whole-operation retry, ``_best_effort`` write-swallowing, class-name/duck dispatch,
duplicate SDK/adapter retry) must be UNREACHABLE from production.

This oracle proves the ACs by OBSERVABLE behaviour, using explicit protocol fakes (AC5)
rather than any production fallback path:

- AC1 — every supported ``(direction, action)`` in ``mutation._VALID_COMBINATIONS`` with
  a typed leaf dispatches to EXACTLY ONE owner (no dual-send), on the typed path and on
  the live batch path.
- AC2 — a valid-but-unowned combo (inbound delete / inbound probe) raises a typed,
  provider-neutral ``UnknownActionError`` BEFORE any effect (zero writes).
- AC3/AC4 — the removed constructs are ABSENT from the source (duck/class-name dispatch,
  the obsolete whole-operation retry around ``delete_issue``) AND the legacy create core
  is never reached from a production create.
- AC6 — neither create route deletes a remote issue; rollback is code/routing reversion
  + remote RE-OBSERVATION, never a remote delete.

The protocol fake mirrors ``mutate/test_create_live_cutover_heldout.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar_reconciler.dispatch_one as dispatch_one
import rebar_reconciler.typed_dispatch as typed_dispatch
from rebar_reconciler.apply_base import _load_errors_module, _load_mutation_module
from rebar_reconciler.apply_handlers import BatchApplyContext, dispatch_mutation
from rebar_reconciler.binding_store import BindingStore

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SRC = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"
APPLIER_PATH = SRC / "applier.py"
BATCH_DISPATCH_PATH = SRC / "batch_dispatch.py"

_mut_mod = _load_mutation_module()
Mutation = _mut_mod.Mutation

_WRITE_METHODS = frozenset(
    {
        "create_issue",
        "update_issue",
        "delete_issue",
        "add_label",
        "remove_label",
        "set_entity_property",
        "add_comment",
        "set_parent",
        "set_relationship",
    }
)


class _ProtocolFake:
    """An explicit, declared-protocol transport fake (AC5): records every physical call.

    Provider-neutral — it stands in for either the Cloud (acli) or DC transport; the
    census asserts the SAME typed owner handles a route regardless of backend.
    ``delete_issue`` is recorded but must NEVER be called on any create/update path.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def create_issue(self, fields):
        self.calls.append(("create_issue", fields))
        return {"key": "CEN-1", "id": "1001"}

    def update_issue(self, key, **fields):
        self.calls.append(("update_issue", key))
        return {"key": key}

    def delete_issue(self, key):
        self.calls.append(("delete_issue", key))
        return {"status": "deleted", "key": key}

    def search_issues(self, jql, *a, **k):
        self.calls.append(("search_issues", jql))
        return []

    def add_label(self, key, label):
        self.calls.append(("add_label", (key, label)))

    def remove_label(self, key, label):
        self.calls.append(("remove_label", (key, label)))

    def set_entity_property(self, key, name, value):
        self.calls.append(("set_entity_property", (key, name)))

    def add_comment(self, key, body):
        self.calls.append(("add_comment", key))
        return {"id": "c-1"}

    def set_parent(self, key, parent):
        self.calls.append(("set_parent", (key, parent)))

    def set_relationship(self, *a, **k):
        self.calls.append(("set_relationship", a))

    @property
    def writes(self) -> list[tuple[str, object]]:
        return [c for c in self.calls if c[0] in _WRITE_METHODS]

    def count(self, method: str) -> int:
        return sum(1 for c in self.calls if c[0] == method)


def _supported_combos():
    return sorted(_mut_mod._VALID_COMBINATIONS, key=lambda p: (p[0].value, p[1].value))


def _ctx(fake, store, tmp_path) -> BatchApplyContext:
    return BatchApplyContext(client=fake, repo_root=tmp_path, pass_id="census", binding_store=store)


def _create_dict(local_id="L-census"):
    return {
        "action": "create",
        "local_id": local_id,
        "fields": {"summary": f"Reconcile {local_id}", "issuetype": {"name": "Task"}},
    }


# ════════════════════════════════════════════════════════════════════════════════
# AC1 — every supported route has EXACTLY ONE typed owner (no dual-send)
# ════════════════════════════════════════════════════════════════════════════════


def test_ac1_typed_census_single_owner_per_supported_route(monkeypatch):
    """AC1: for every supported ``(direction, action)`` that has a typed leaf, the typed
    dispatch entry point ``typed_dispatch._apply_typed`` routes to EXACTLY ONE owner.

    Observable: each leaf is wrapped to record its invocation, then the real dispatcher is
    driven per combo and must record exactly one owner — proving no dual-send and that a
    single typed table owns every route. Wrapped leaves issue NO physical writes.
    """
    invoked: list[tuple] = []
    real_leaves = dict(typed_dispatch._LEAVES)
    assert real_leaves, "typed dispatch table must be non-empty"

    def _wrap(key):
        def _rec(mutation, **kwargs):
            invoked.append(key)
            return object()

        return _rec

    monkeypatch.setattr(typed_dispatch, "_LEAVES", {k: _wrap(k) for k in real_leaves})
    fake = _ProtocolFake()
    covered = 0
    for direction, action in _supported_combos():
        key = (direction, action)
        if key not in real_leaves:
            continue  # valid-but-unowned combos are AC2's domain
        invoked.clear()
        mutation = Mutation(direction, action, target="CEN-1", payload={}, provenance={})
        typed_dispatch._apply_typed(mutation, client=fake, repo_root=None)
        assert invoked == [key], f"{key} dispatched to {invoked} (expected exactly one owner)"
        covered += 1
    assert covered == len(real_leaves), "every registered leaf must be exercised by the census"
    assert fake.writes == [], "the ownership census must not perform physical writes"


def test_ac1_outbound_batch_families_single_physical_owner(tmp_path, monkeypatch):
    """AC1 on the LIVE production batch path: ``dispatch_mutation`` drives create/update/
    delete each through EXACTLY ONE physical write of the expected kind — no dual-send.
    """
    monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)

    fake = _ProtocolFake()
    store = BindingStore(tmp_path / ".t-create")
    dispatch_mutation(_create_dict("L-c"), _ctx(fake, store, tmp_path))
    assert fake.count("create_issue") == 1
    assert fake.count("delete_issue") == 0  # AC6: a create never remote-deletes

    fake = _ProtocolFake()
    store = BindingStore(tmp_path / ".t-update")
    dispatch_mutation(
        {"action": "update", "key": "CEN-2", "local_id": "L-u", "fields": {"summary": "next"}},
        _ctx(fake, store, tmp_path),
    )
    assert fake.count("update_issue") == 1
    assert fake.count("delete_issue") == 0  # AC6: an update never remote-deletes

    fake = _ProtocolFake()
    store = BindingStore(tmp_path / ".t-delete")
    dispatch_mutation({"action": "delete", "key": "CEN-3"}, _ctx(fake, store, tmp_path))
    assert fake.count("delete_issue") == 1


# ════════════════════════════════════════════════════════════════════════════════
# AC2 — unsupported production action → typed, provider-neutral error BEFORE any effect
# ════════════════════════════════════════════════════════════════════════════════


def test_ac2_unsupported_action_typed_error_before_any_effect():
    """AC2: a VALID combo with NO typed leaf (inbound delete / inbound probe) raises a
    typed, provider-neutral ``UnknownActionError`` BEFORE any effect — zero writes.
    """
    errs = _load_errors_module()
    unsupported = [c for c in _supported_combos() if c not in typed_dispatch._LEAVES]
    assert unsupported, "expected at least one valid-but-unowned combo (inbound delete/probe)"
    for direction, action in unsupported:
        fake = _ProtocolFake()
        mutation = Mutation(direction, action, target="CEN-9", payload={}, provenance={})
        with pytest.raises(errs.UnknownActionError):
            typed_dispatch._apply_typed(mutation, client=fake, repo_root=None)
        assert fake.writes == [], f"{(direction, action)} performed a write before erroring"


# ════════════════════════════════════════════════════════════════════════════════
# AC3 / AC4 — removed constructs absent (static/string poison) + unreachable (runtime)
# ════════════════════════════════════════════════════════════════════════════════


def test_ac4_duck_and_class_name_dispatch_removed_from_applier():
    """AC4: the production class-name / duck-typed dispatch fallback is GONE from
    applier.py; typed dispatch is selected by an ``isinstance`` check ONLY.
    """
    src = APPLIER_PATH.read_text()
    assert 'type(mutations).__name__ == "Mutation"' not in src
    assert 'type(m).__name__ == "Mutation"' not in src
    assert '__name__ == "Mutation"' not in src
    assert "isinstance(mutations, mut_mod.Mutation)" in src


def test_ac3_obsolete_whole_operation_delete_retry_removed():
    """AC3/AC4: the obsolete whole-operation retry route wrapping ``client.delete_issue``
    in ``delete_one`` is GONE — the adapter owns retry (DC retries connection errors
    internally; Cloud deliberately issues a delete single-attempt). ``delete_one`` now
    calls the transport directly.
    """
    src = BATCH_DISPATCH_PATH.read_text()
    assert "_call_with_retry(client.delete_issue" not in src
    assert "client.delete_issue(" in src


def test_ac3_legacy_create_core_unreachable_from_production(tmp_path, monkeypatch):
    """AC3: production create never reaches the legacy write-ahead core.

    Spy the legacy create core and drive a production create through the live dispatch
    with the DEFAULT (coordinated) route — the coordinated owner runs and the legacy
    core is NEVER invoked.
    """
    monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    reached: list[bool] = []
    real = dispatch_one._legacy_create_core

    def _spy(*args, **kwargs):
        reached.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(dispatch_one, "_legacy_create_core", _spy)
    fake = _ProtocolFake()
    store = BindingStore(tmp_path / ".t-legacy-core")
    dispatch_mutation(_create_dict("L-nolegacy"), _ctx(fake, store, tmp_path))
    assert reached == [], "production create must not reach the legacy write-ahead core"
    assert fake.count("create_issue") == 1  # the coordinated typed owner ran instead


# ════════════════════════════════════════════════════════════════════════════════
# AC6 — rollback = code/routing reversion + remote RE-OBSERVATION, never remote delete
# ════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("route", [None, "legacy"])
def test_ac6_no_remote_delete_on_any_create_route(tmp_path, monkeypatch, route):
    """AC6: NEITHER create route (coordinated default nor legacy rollback) deletes a
    remote issue on a create — proven observably: ``delete_issue`` is never called.
    """
    if route is None:
        monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    else:
        monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", route)
    fake = _ProtocolFake()
    store = BindingStore(tmp_path / f".t-{route}")
    dispatch_mutation(_create_dict(f"L-{route}"), _ctx(fake, store, tmp_path))
    assert fake.count("create_issue") == 1
    assert fake.count("delete_issue") == 0


def test_ac6_rollback_guidance_reobserve_never_remote_delete():
    """AC6: the create ownership guidance documents rollback as re-observation, and that a
    successfully-created remote issue is NEVER deleted (no delete seam) — code/routing
    reversion + re-observation, never a remote delete.
    """
    from rebar_reconciler import create_coordinator

    doc = (create_coordinator.__doc__ or "").lower()
    assert "re-observe" in doc, "guidance must require re-observation on ambiguous create"
    assert "never deletes" in doc, "guidance must state a created remote issue is never deleted"
