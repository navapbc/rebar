"""Reconcile-pass preflight: mapped project + legacy_default visibility (ticket a011).

Before a reconcile pass mutates a live Jira, EVERY project the pass could touch —
``projects_store.read_projects().keys()``, the configured ``query_project`` fallback when
that mapping is empty, and ``legacy_default`` when set — must be visible to the bridge bot.
A mapped-but-nonexistent/decommissioned/permission-lost key is syntactically valid and only
fails deep in fan-out (the 05b8 incident class); a wrong ``legacy_default`` drives a mass
CREATE storm into the wrong project. The preflight fails fast, before any outbound mutation.

These tests inject a FAKE transport — no live Jira. They cover both the reusable
``access_check`` helper (the single source of truth ticket 9702's fsck diagnostic reuses)
and the ``__main__.run_pass_result`` wiring that aborts the pass before
``reconcile.reconcile_once`` (the only outbound-mutation call).
"""

from __future__ import annotations

import importlib.util
import sys
import types
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ENGINE_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
MAIN_PATH = ENGINE_DIR / "rebar_reconciler" / "__main__.py"

if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))


# ---------------------------------------------------------------------------
# Module loaders
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def access_check():
    from rebar_reconciler import access_check as mod

    return mod


@pytest.fixture(scope="module")
def main_mod():
    if not MAIN_PATH.exists():
        pytest.fail(f"__main__.py not found at {MAIN_PATH}")
    spec = importlib.util.spec_from_file_location("rebar_reconciler.__main__", MAIN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rebar_reconciler.__main__"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _write_projects(repo_root: Path, *, projects: dict[str, list[str]], legacy_default=None):
    d = repo_root / ".tickets-tracker" / ".bridge_state"
    d.mkdir(parents=True, exist_ok=True)
    record = {
        "version": 1,
        "legacy_default": legacy_default,
        "projects": {k: {"repos": v} for k, v in projects.items()},
    }
    import json

    (d / "projects.json").write_text(json.dumps(record), encoding="utf-8")


class FakeProjectSearchClient:
    """A fake AcliClient exposing only ``_direct_rest_get`` for /project/search.

    Records any would-be mutation call so tests can prove the preflight made none.
    """

    def __init__(self, visible_keys=(), *, pages=None, raise_exc=None):
        self._visible = list(visible_keys)
        self._pages = pages
        self._raise = raise_exc
        self.get_paths: list[str] = []
        self.mutations: list[tuple] = []

    def _direct_rest_get(self, path: str):
        self.get_paths.append(path)
        if self._raise is not None:
            raise self._raise
        if self._pages is not None:
            import urllib.parse as up

            q = up.parse_qs(up.urlparse(path).query)
            start = int(q.get("startAt", ["0"])[0])
            for pg in self._pages:
                if pg.get("startAt") == start:
                    return pg
            return {"values": [], "isLast": True, "startAt": start}
        return {
            "values": [{"key": k} for k in self._visible],
            "isLast": True,
            "startAt": 0,
            "total": len(self._visible),
        }

    # Mutation surface — must remain unused by the preflight.
    def create_issue(self, *a, **k):
        self.mutations.append(("create", a, k))
        return {}

    def delete_issue(self, *a, **k):
        self.mutations.append(("delete", a, k))
        return {}

    def update_issue(self, *a, **k):
        self.mutations.append(("update", a, k))
        return {}


# ===========================================================================
# Helper-level tests (the reusable access_check surface)
# ===========================================================================


def test_all_visible_returns_ok(access_check, tmp_path):
    _write_projects(tmp_path, projects={"GOOD": ["r"]})
    client = FakeProjectSearchClient(["GOOD", "OTHER"])
    result = access_check.check_mapped_project_visibility(tmp_path, probe=client)
    assert result.status == "ok"
    assert result.missing == ()
    assert client.mutations == []  # a visibility probe never mutates
    # enforce is a no-op on ok
    assert access_check.enforce_mapped_project_visibility(tmp_path, probe=client).status == "ok"


def test_missing_key_aborts_naming_bad(access_check, tmp_path):
    _write_projects(tmp_path, projects={"GOOD": ["r"], "BAD": ["r"]})
    client = FakeProjectSearchClient(["GOOD"])
    result = access_check.check_mapped_project_visibility(tmp_path, probe=client)
    assert result.status == "missing"
    assert result.missing == ("BAD",)
    with pytest.raises(access_check.ProjectVisibilityError) as exc:
        access_check.enforce_mapped_project_visibility(tmp_path, probe=client)
    assert "BAD" in str(exc.value)
    assert client.mutations == []


def test_legacy_default_included_and_caught(access_check, tmp_path):
    _write_projects(tmp_path, projects={"GOOD": ["r"]}, legacy_default="WRONG")
    client = FakeProjectSearchClient(["GOOD"])
    with pytest.raises(access_check.ProjectVisibilityError) as exc:
        access_check.enforce_mapped_project_visibility(tmp_path, probe=client)
    assert "WRONG" in str(exc.value)


def test_legacy_default_visible_ok(access_check, tmp_path):
    _write_projects(tmp_path, projects={"GOOD": ["r"]}, legacy_default="GOOD")
    client = FakeProjectSearchClient(["GOOD"])
    assert access_check.check_mapped_project_visibility(tmp_path, probe=client).status == "ok"


def test_empty_projects_uses_query_project_fallback(access_check, tmp_path):
    # Empty mapping — the fetcher falls back to the single configured query_project.
    _write_projects(tmp_path, projects={}, legacy_default=None)
    client = FakeProjectSearchClient(["GOOD"])  # FALL not visible
    with pytest.raises(access_check.ProjectVisibilityError) as exc:
        access_check.enforce_mapped_project_visibility(tmp_path, probe=client, query_project="FALL")
    assert "FALL" in str(exc.value)
    # visible fallback → ok
    client2 = FakeProjectSearchClient(["FALL"])
    assert (
        access_check.check_mapped_project_visibility(
            tmp_path, probe=client2, query_project="FALL"
        ).status
        == "ok"
    )


def test_nothing_to_check_never_probes(access_check, tmp_path):
    _write_projects(tmp_path, projects={}, legacy_default=None)
    client = FakeProjectSearchClient([])
    result = access_check.check_mapped_project_visibility(
        tmp_path, probe=client, query_project=None
    )
    assert result.status == "ok"
    assert client.get_paths == []  # no required keys → no probe call


def test_transport_error_fails_closed(access_check, tmp_path):
    _write_projects(tmp_path, projects={"GOOD": ["r"]})
    boom = urllib.error.HTTPError("http://x/rest/api/3/project/search", 403, "Forbidden", {}, None)
    client = FakeProjectSearchClient(raise_exc=boom)
    result = access_check.check_mapped_project_visibility(tmp_path, probe=client)
    assert result.status == "transport_unavailable"
    with pytest.raises(access_check.TransportUnavailableError):
        access_check.enforce_mapped_project_visibility(tmp_path, probe=client)
    # transport-unavailable is a DIFFERENT type than project-not-visible
    assert not issubclass(
        access_check.TransportUnavailableError, access_check.ProjectVisibilityError
    )
    assert not issubclass(
        access_check.ProjectVisibilityError, access_check.TransportUnavailableError
    )


def test_client_without_probe_accessor_is_skipped(access_check, tmp_path):
    _write_projects(tmp_path, projects={"GOOD": ["r"]})
    client = types.SimpleNamespace()  # no _direct_rest_get (Data Center transport shape)
    result = access_check.check_mapped_project_visibility(tmp_path, probe=client)
    assert result.status == "skipped"
    # enforce does not raise on a skip
    assert (
        access_check.enforce_mapped_project_visibility(tmp_path, probe=client).status == "skipped"
    )


def test_pagination_collects_all_pages(access_check, tmp_path):
    _write_projects(tmp_path, projects={"P1": ["r"], "P2": ["r"], "P3": ["r"]})
    pages = [
        {"startAt": 0, "values": [{"key": "P1"}, {"key": "P2"}], "isLast": False, "total": 3},
        {"startAt": 2, "values": [{"key": "P3"}], "isLast": True, "total": 3},
    ]
    client = FakeProjectSearchClient(pages=pages)
    result = access_check.check_mapped_project_visibility(tmp_path, probe=client)
    assert result.status == "ok", result
    assert {"P1", "P2", "P3"} <= result.visible


def test_pagination_non_termination_fails_closed(access_check, tmp_path):
    _write_projects(tmp_path, projects={"GOOD": ["r"]})

    class NeverLastClient(FakeProjectSearchClient):
        def _direct_rest_get(self, path):
            self.get_paths.append(path)
            return {"values": [{"key": "X"}], "isLast": False}  # never terminates, no total

    client = NeverLastClient()
    result = access_check.check_mapped_project_visibility(tmp_path, probe=client)
    assert result.status == "transport_unavailable"


# ===========================================================================
# Wiring-level tests (__main__.run_pass_result aborts BEFORE reconcile_once)
# ===========================================================================


def _seed_fake_access_check(monkeypatch, result=None, *, check=None):
    fake = types.ModuleType("rebar_reconciler.access_check")
    fake.check_mapped_project_visibility = check or MagicMock(return_value=result)
    monkeypatch.setitem(sys.modules, "rebar_reconciler.access_check", fake)
    return fake


def _stub_reconcile(return_value=None):
    stub = types.ModuleType("stub_reconcile")
    rv = return_value if return_value is not None else {"pass_id": "p", "mutation_count": 0}
    stub.reconcile_once = MagicMock(return_value=rv)
    return stub


def _patch_cloud_backend(monkeypatch, *, backend="jira", query_project="FALL"):
    monkeypatch.setattr(
        "rebar.config.load_config",
        lambda: types.SimpleNamespace(reconciler=types.SimpleNamespace(backend=backend)),
    )
    monkeypatch.setattr(
        "rebar_reconciler._backend_registry.select_backend",
        lambda config: types.SimpleNamespace(query_project=query_project),
    )


def _result(status, **kw):
    return types.SimpleNamespace(
        status=status, missing=kw.get("missing", ()), detail=kw.get("detail", "")
    )


def test_wiring_missing_aborts_before_reconcile(main_mod, monkeypatch, tmp_path):
    _seed_fake_access_check(monkeypatch, _result("missing", missing=("BAD",)))
    _patch_cloud_backend(monkeypatch)
    stub = _stub_reconcile()
    monkeypatch.setattr(
        main_mod, "_try_load_step", lambda name: stub if name == "reconcile" else None
    )

    res = main_mod.run_pass_result(repo_root=tmp_path, route=None)

    stub.reconcile_once.assert_not_called()
    assert res.disposition is main_mod._Disposition.OPERATIONAL_FAILURE
    assert "BAD" in (res.canonical_message or "")


def test_wiring_transport_unavailable_aborts(main_mod, monkeypatch, tmp_path):
    _seed_fake_access_check(monkeypatch, _result("transport_unavailable", detail="403 Forbidden"))
    _patch_cloud_backend(monkeypatch)
    stub = _stub_reconcile()
    monkeypatch.setattr(
        main_mod, "_try_load_step", lambda name: stub if name == "reconcile" else None
    )

    res = main_mod.run_pass_result(repo_root=tmp_path, route=None)

    stub.reconcile_once.assert_not_called()
    assert res.disposition is main_mod._Disposition.OPERATIONAL_FAILURE


def test_wiring_all_visible_proceeds(main_mod, monkeypatch, tmp_path):
    _seed_fake_access_check(monkeypatch, _result("ok"))
    _patch_cloud_backend(monkeypatch)
    stub = _stub_reconcile()
    monkeypatch.setattr(
        main_mod, "_try_load_step", lambda name: stub if name == "reconcile" else None
    )

    res = main_mod.run_pass_result(repo_root=tmp_path, route=None)

    stub.reconcile_once.assert_called_once()
    assert res.disposition is main_mod._Disposition.CONVERGED


def test_wiring_preview_route_skips_probe(main_mod, monkeypatch, tmp_path):
    fake = _seed_fake_access_check(monkeypatch, _result("missing", missing=("BAD",)))
    _patch_cloud_backend(monkeypatch)
    stub = _stub_reconcile(return_value={"no_write": True, "mutation_count": 0})
    monkeypatch.setattr(
        main_mod, "_try_load_step", lambda name: stub if name == "reconcile" else None
    )

    main_mod.run_pass_result(repo_root=tmp_path, route="preview")

    fake.check_mapped_project_visibility.assert_not_called()
    stub.reconcile_once.assert_called_once()


def test_wiring_dry_run_skips_probe(main_mod, monkeypatch, tmp_path):
    from rebar_reconciler import mode as mode_mod

    fake = _seed_fake_access_check(monkeypatch, _result("missing", missing=("BAD",)))
    _patch_cloud_backend(monkeypatch)
    stub = _stub_reconcile()
    monkeypatch.setattr(
        main_mod, "_try_load_step", lambda name: stub if name == "reconcile" else None
    )

    main_mod.run_pass_result(repo_root=tmp_path, target_mode=mode_mod.Mode.DRY_RUN, route=None)

    fake.check_mapped_project_visibility.assert_not_called()
    stub.reconcile_once.assert_called_once()


def test_wiring_non_cloud_backend_skips_probe(main_mod, monkeypatch, tmp_path):
    fake = _seed_fake_access_check(monkeypatch, _result("missing", missing=("BAD",)))
    _patch_cloud_backend(monkeypatch, backend="jira-datacenter")
    stub = _stub_reconcile()
    monkeypatch.setattr(
        main_mod, "_try_load_step", lambda name: stub if name == "reconcile" else None
    )

    main_mod.run_pass_result(repo_root=tmp_path, route=None)

    fake.check_mapped_project_visibility.assert_not_called()
    stub.reconcile_once.assert_called_once()
