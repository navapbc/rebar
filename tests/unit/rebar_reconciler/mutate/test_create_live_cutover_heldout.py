"""HELD-OUT: the S4 T3 create cutover on the LIVE outbound path (REB-3115, AC6).

S4 T1/T2 built the pure create + containment slices and S4 T3's earlier patch composed
them (``create_route``) and cut over the TYPED create leaf. But production outbound
creates do NOT flow through the typed leaf — they flow through the batch path
``applier.apply(list)`` → ``_apply_batch`` → ``_apply_one`` → ``apply_handlers.handle_create``
→ ``dispatch_one.create_one``. This oracle pins the cutover on THAT live path: it drives
the real batch entry point ``applier.apply([create_dict], ...)`` and the real dispatch
step ``apply_handlers.handle_create(mutation, ctx)`` (the SINGLE ``create_route`` selector
consumption point) and asserts OBSERVABLE outcomes only — the create-call count, the
forward+reverse binding, the outcome dict, ``deferred_creates``, and that the issue is
NEVER deleted.

Both routes (coordinator default + legacy rollback) run EXACTLY ONE physical create per
mutation (no dual-send). The legacy route must stay byte-identical (it is S5's rollback).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from rebar_reconciler.apply_handlers import BatchApplyContext, dispatch_mutation, handle_create
from rebar_reconciler.binding_store import BindingPersistError, BindingStore

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeClient:
    """A transport-shaped fake recording every physical call.

    ``search_result`` seeds the dedup JQL / observe hit list; ``create_exc`` /
    ``label_exc`` inject a terminal create failure or a rebar-id identity-label failure.
    ``delete_issue`` is recorded but must NEVER be called on any create path (bug 387d).
    """

    def __init__(self) -> None:
        self.creates = 0
        self.searches = 0
        self.labels: list[tuple[str, str]] = []
        self.props: list[tuple] = []
        self.comments: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.search_result: list[dict] = []
        self.create_result: dict = {"key": "DIG-1", "id": "1001"}
        self.create_exc: BaseException | None = None
        self.label_exc: BaseException | None = None

    def search_issues(self, jql, *a, **k):
        self.searches += 1
        return list(self.search_result)

    def create_issue(self, fields):
        self.creates += 1
        if self.create_exc is not None:
            raise self.create_exc
        return dict(self.create_result)

    def add_label(self, key, label):
        if self.label_exc is not None and label.startswith("rebar-id:"):
            raise self.label_exc
        self.labels.append((key, label))

    def set_entity_property(self, key, name, value):
        self.props.append((key, name, value))

    def add_comment(self, key, body):
        self.comments.append((key, body))
        return {"id": "c-1"}

    def delete_issue(self, key):  # pragma: no cover - must never run on a create path
        self.deletes.append(key)


def _mutation(local_id: str = "L-1", **extra) -> dict:
    m = {
        "action": "create",
        "local_id": local_id,
        "fields": {"summary": f"Reconcile {local_id}", "issuetype": {"name": "Task"}},
    }
    m.update(extra)
    return m


def _ctx(client, store, tmp_path) -> BatchApplyContext:
    return BatchApplyContext(client=client, repo_root=tmp_path, pass_id="p-t3", binding_store=store)


# ════════════════════════════════════════════════════════════════════════════════
# handle_create — the SINGLE create_route selector consumption point on the batch path
# ════════════════════════════════════════════════════════════════════════════════


def test_coordinator_route_runs_coordinated_core_one_create_binding_confirmed(
    tmp_path, monkeypatch
):
    """Default (unset env) → coordinated core: EXACTLY ONE create, forward+reverse binding
    confirmed, one remote issue, NO delete."""
    monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    client = _FakeClient()
    store = BindingStore(tmp_path / ".tickets-tracker")
    result = handle_create(_mutation("L-coord"), _ctx(client, store, tmp_path))

    assert client.creates == 1
    assert client.deletes == []
    # outcome dict carries the create response dict as result.
    assert result.outcome["result"] == {"key": "DIG-1", "id": "1001"}
    # Forward + reverse binding confirmed via the coordinated containment.
    assert store.get_jira_key("L-coord") == "DIG-1"
    assert store.get_local_id("DIG-1") == "L-coord"
    assert ("DIG-1", "rebar-id:L-coord") in client.labels
    assert ("DIG-1", "local_id", "L-coord") in client.props


def test_legacy_route_runs_legacy_core_byte_identical(tmp_path, monkeypatch):
    """Legacy rollback value → legacy core: same observable create + confirmed binding.
    Behaviour is byte-identical to the coordinated happy path (this is the rollback)."""
    monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", "legacy")
    client = _FakeClient()
    store = BindingStore(tmp_path / ".tickets-tracker")
    result = handle_create(_mutation("L-legacy"), _ctx(client, store, tmp_path))

    assert client.creates == 1
    assert client.deletes == []
    assert result.outcome["result"] == {"key": "DIG-1", "id": "1001"}
    assert store.get_jira_key("L-legacy") == "DIG-1"
    assert store.get_local_id("DIG-1") == "L-legacy"


@pytest.mark.parametrize("route", [None, "legacy"])
def test_success_counts_rest_and_sets_result_both_routes(tmp_path, monkeypatch, route):
    """(a) A successful create counts one REST call and sets outcome['result'] on BOTH
    routes (shared prelude/postlude)."""
    if route is None:
        monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    else:
        monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", route)
    client = _FakeClient()
    ctx = _ctx(client, BindingStore(tmp_path / ".tickets-tracker"), tmp_path)
    result = handle_create(_mutation("L-ok"), ctx)
    assert ctx.rest_calls == 1
    assert result.outcome["result"]["key"] == "DIG-1"


@pytest.mark.parametrize("route", [None, "legacy"])
def test_create_404_soft_fails_pass_continues_both_routes(tmp_path, monkeypatch, route):
    """(b) A create raising HTTPError 404 → per-mutation soft-fail (recorded, pass not
    aborted) via the shared dispatch backstop, on BOTH routes."""
    if route is None:
        monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    else:
        monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", route)
    client = _FakeClient()
    client.create_exc = urllib.error.HTTPError("u", 404, "gone", {}, None)  # type: ignore[arg-type]
    ctx = _ctx(client, BindingStore(tmp_path / ".t"), tmp_path)
    result = handle_create(_mutation("L-404"), ctx)
    assert result.soft_failed is True
    assert "stale-binding-404" in result.outcome["error"]
    assert client.deletes == []


@pytest.mark.parametrize("route", [None, "legacy"])
def test_non_404_httperror_propagates_both_routes(tmp_path, monkeypatch, route):
    """(c) A non-404 HTTPError propagates fail-fast (the applier re-raises it) on BOTH
    routes — the coordinated core re-raises the ORIGINAL HTTPError object so the 404
    taxonomy is byte-identical."""
    if route is None:
        monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    else:
        monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", route)
    client = _FakeClient()
    client.create_exc = urllib.error.HTTPError("u", 500, "boom", {}, None)  # type: ignore[arg-type]
    with pytest.raises(urllib.error.HTTPError) as exc:
        handle_create(_mutation("L-500"), _ctx(client, BindingStore(tmp_path / ".t"), tmp_path))
    assert exc.value.code == 500
    assert client.deletes == []


@pytest.mark.parametrize("route", [None, "legacy"])
def test_landed_but_label_fails_alerts_no_delete_no_postlude(tmp_path, monkeypatch, route):
    """(d) The create landed but the rebar-id identity label write fails → a BRIDGE_ALERT
    is emitted, the issue is NEVER deleted (bug 387d), the error is raised, the key is
    left keyed-pending, and the postlude (user labels/comments) does NOT run. Identical on
    BOTH routes."""
    if route is None:
        monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    else:
        monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", route)
    client = _FakeClient()
    client.label_exc = RuntimeError("field off screen")
    store = BindingStore(tmp_path / ".tickets-tracker")
    mutation = _mutation(
        "L-lblfail",
        labels=[{"action": "add", "label": "user-label"}],
        comments=[{"body": "should not post"}],
    )
    with pytest.raises(RuntimeError, match="field off screen"):
        handle_create(mutation, _ctx(client, store, tmp_path))

    assert client.deletes == []  # NEVER delete a created issue
    assert client.creates == 1
    # Key recorded on a keyed-pending binding (create landed), never confirmed.
    assert store.get_jira_key("L-lblfail") == "DIG-1"
    assert store.get_local_id("DIG-1") is None  # not confirmed → no reverse binding
    # Postlude skipped: no user label, no comment dispatched.
    assert ("DIG-1", "user-label") not in client.labels
    assert client.comments == []
    # A BRIDGE_ALERT was staged under the tracker root.
    alerts = list((tmp_path / ".tickets-tracker").rglob("*-BRIDGE_ALERT.json"))
    assert alerts, "expected a BRIDGE_ALERT for the identity-write failure"
    payload = json.loads(alerts[0].read_text())
    assert payload["data"]["tag"] == "create-identity-write-failed"


@pytest.mark.parametrize("route", [None, "legacy"])
def test_persist_failure_raises_binding_persist_error_no_create(tmp_path, monkeypatch, route):
    """(e) A write-ahead bind_pending persist failure raises BindingPersistError and skips
    the create entirely, on BOTH routes (same message shape)."""
    if route is None:
        monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    else:
        monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", route)
    client = _FakeClient()

    class _SaveFailsStore(BindingStore):
        def save(self) -> None:
            raise OSError("disk full")

    store = _SaveFailsStore(tmp_path / ".tickets-tracker")
    with pytest.raises(BindingPersistError, match="bind_pending persist failed"):
        handle_create(_mutation("L-persist"), _ctx(client, store, tmp_path))
    assert client.creates == 0
    assert client.deletes == []


def test_commit_unknown_defers_no_second_create_no_false_applied(tmp_path, monkeypatch):
    """(f) Coordinator route: an ambiguous create (timeout) whose re-observation is
    inconclusive → DEFER. The mutation is appended to deferred_creates, NO second create is
    issued, the outcome is not a false 'applied' (result is None), and nothing is deleted."""
    monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    client = _FakeClient()
    client.create_exc = TimeoutError("ack lost")  # ambiguous → re-observe
    client.search_result = []  # observe inconclusive
    store = BindingStore(tmp_path / ".tickets-tracker")
    ctx = _ctx(client, store, tmp_path)
    mutation = _mutation("L-unknown")
    result = handle_create(mutation, ctx)

    assert client.creates == 1  # exactly one physical create attempt, no blind replay
    assert client.deletes == []
    assert result.outcome["result"] is None  # not a false-applied
    assert ctx.rest_calls == 0  # a deferred create consumes no REST budget
    assert mutation in ctx.deferred_creates  # queued for a later convergence pass


@pytest.mark.parametrize("route", [None, "legacy"])
def test_dedup_hit_short_circuits_before_core_both_routes(tmp_path, monkeypatch, route):
    """Shared prelude: a dedup JQL hit short-circuits BEFORE the create core on BOTH
    routes — no create, a dedup sentinel, and the mapping confirmed."""
    if route is None:
        monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    else:
        monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", route)
    client = _FakeClient()
    client.search_result = [{"key": "DIG-DUP"}]
    store = BindingStore(tmp_path / ".tickets-tracker")
    result = handle_create(_mutation("L-dedup"), _ctx(client, store, tmp_path))
    assert client.creates == 0
    assert result.outcome["result"]["status"] == "dedup-create-skipped"
    assert result.outcome["result"]["key"] == "DIG-DUP"


@pytest.mark.parametrize("route", [None, "legacy"])
def test_budget_defer_short_circuits_before_core_both_routes(tmp_path, monkeypatch, route):
    """Shared prelude: at/over the REST-call budget the create is deferred BEFORE any core
    on BOTH routes — no create, no search, mutation queued."""
    if route is None:
        monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    else:
        monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", route)
    client = _FakeClient()
    store = BindingStore(tmp_path / ".tickets-tracker")
    ctx = _ctx(client, store, tmp_path)
    ctx.rest_calls = 10_000  # far over the budget
    mutation = _mutation("L-budget")
    result = handle_create(mutation, ctx)
    assert client.creates == 0
    assert client.searches == 0
    assert result.outcome["result"] is None
    assert mutation in ctx.deferred_creates


def test_no_dual_send_dispatch_mutation_issues_exactly_one_create(tmp_path, monkeypatch):
    """No-dual-send invariant on the live dispatch table: dispatch_mutation('create') runs
    create_one exactly once, whose middle core is EITHER coordinated OR legacy — never both.
    Exactly one physical create_issue under the coordinator route."""
    monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    client = _FakeClient()
    store = BindingStore(tmp_path / ".tickets-tracker")
    dispatch_mutation(_mutation("L-single"), _ctx(client, store, tmp_path))
    assert client.creates == 1
    assert client.deletes == []


# ════════════════════════════════════════════════════════════════════════════════
# The REAL batch entry point: applier.apply([create_dict], ...)
# ════════════════════════════════════════════════════════════════════════════════


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True)
    (path / ".dummy").write_text("seed")
    subprocess.run(["git", "-C", str(path), "add", ".dummy"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "seed"], check=True)


def _load_applier(name: str):
    spec = importlib.util.spec_from_file_location(name, APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _read_manifest(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.mark.parametrize("route", [None, "legacy"])
def test_applier_apply_live_batch_create_one_issue_one_binding(tmp_path, monkeypatch, route):
    """The true production entry point: ``applier.apply([create_dict], ...)`` issues EXACTLY
    ONE physical create, confirms a forward+reverse binding, never deletes, and records the
    create response as the mutation outcome — on BOTH routes."""
    if route is None:
        monkeypatch.delenv("REBAR_RECONCILER_CREATE_ROUTE", raising=False)
    else:
        monkeypatch.setenv("REBAR_RECONCILER_CREATE_ROUTE", route)
    applier = _load_applier(f"applier_live_{route}")
    client = _FakeClient()
    monkeypatch.setattr(applier, "_load_acli", lambda: client)
    _init_git_repo(tmp_path)
    store = BindingStore(tmp_path / ".tickets-tracker")

    manifest_path = applier.apply(
        [_mutation("L-live")], "pass-live", repo_root=tmp_path, binding_store=store
    )

    assert client.creates == 1
    assert client.deletes == []
    assert store.get_jira_key("L-live") == "DIG-1"
    assert store.get_local_id("DIG-1") == "L-live"
    manifest = _read_manifest(manifest_path)
    (outcome,) = manifest["mutations"]
    assert outcome["result"]["key"] == "DIG-1"
    assert outcome.get("error") is None
