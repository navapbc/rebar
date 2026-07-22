"""Bug 626d: cross-project safety guard on the outbound apply path.

A store carrying stale bindings/labels for a different Jira project must NOT push
outbound updates/deletes at that project's issues. ``_apply_batch`` performs a
pre-flight scan (before any Jira write) and raises ``CrossProjectTargetError`` —
fail-closed — when any outbound update/delete targets a project other than the
configured ``jira.project``. Creates (local-id placeholder key) and inbound
mutations are exempt.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR / "rebar_reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR / "rebar_reconciler" / "adapters" / "jira" / "acli.py"

# Bootstrap the rebar_reconciler package namespace + the submodules acli.py imports
# at load time, so the importlib loader chain resolves under any cwd (mirrors the
# sibling applier tests).
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if "rebar_reconciler" not in sys.modules:
    import types as _types

    _dr = _types.ModuleType("rebar_reconciler")
    _dr.__path__ = [str(SCRIPTS_DIR / "rebar_reconciler")]
    sys.modules["rebar_reconciler"] = _dr
for _sub in ("adf", "comment_limits"):
    _key = f"rebar_reconciler.adapters.jira.{_sub}"
    if _key not in sys.modules:
        _spec = importlib.util.spec_from_file_location(
            _key, SCRIPTS_DIR / "rebar_reconciler" / "adapters" / "jira" / f"{_sub}.py"
        )
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[_key] = _mod
        _spec.loader.exec_module(_mod)  # type: ignore[union-attr]


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier_mod() -> Iterator[ModuleType]:
    name = "applier_cross_project_guard"
    mod = _load_module(name, APPLIER_PATH)
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def acli_mod() -> Iterator[ModuleType]:
    name = "acli_cross_project_guard"
    mod = _load_module(name, ACLI_PATH)
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


def _make_fake_acli(client: MagicMock) -> MagicMock:
    # S4: _load_acli returns the transport directly.
    return client


def _update(key: str, local_id: str = "loc") -> dict:
    return {
        "direction": "outbound",
        "action": "update",
        "key": key,
        "fields": {"summary": "x"},
        "local_id": local_id,
    }


# --- pure helper unit tests ------------------------------------------------


def test_cross_project_targets_flags_only_foreign_update_delete(applier_mod):
    muts = [
        _update("DIG-100"),  # foreign update -> flagged
        {"direction": "outbound", "action": "delete", "key": "DIG-200", "local_id": "l"},
        _update("REB-1"),  # same project -> ok
        {"direction": "outbound", "action": "create", "key": "REB-local-x", "local_id": "l"},
        # create is exempt even with a foreign-looking key (it's a placeholder):
        {"direction": "outbound", "action": "create", "key": "DIG-999", "local_id": "l"},
        # inbound is exempt:
        {"direction": "inbound", "action": "update", "key": "DIG-5", "local_id": "l"},
    ]
    offenders = applier_mod._cross_project_targets(muts, "REB")
    keys = {k for k, _ in offenders}
    assert keys == {"DIG-100", "DIG-200"}, offenders


def test_cross_project_targets_disabled_when_no_project(applier_mod):
    # Empty configured project disables the check (never fires on unconfigured shims).
    assert applier_mod._cross_project_targets([_update("DIG-1")], "") == []


# --- end-to-end: the guard fires before any Jira write ---------------------


def test_apply_refuses_foreign_project_before_any_write(
    applier_mod, acli_mod, tmp_path, monkeypatch
):
    """With jira.project=REB, an outbound update at DIG-100 raises
    CrossProjectTargetError and NO update_issue call is made."""
    monkeypatch.setenv("JIRA_PROJECT", "REB")
    fake_client = MagicMock()
    with patch.object(applier_mod, "_load_acli", return_value=_make_fake_acli(fake_client)):
        with pytest.raises(applier_mod.CrossProjectTargetError) as exc:
            applier_mod.apply([_update("DIG-100")], f"xproj-{int(time.time())}", repo_root=tmp_path)
    assert "DIG-100" in str(exc.value)
    assert "REB" in str(exc.value)
    fake_client.update_issue.assert_not_called()


def test_apply_allows_matching_project(applier_mod, acli_mod, tmp_path, monkeypatch):
    """A same-project (REB) outbound update is not blocked by the guard."""
    monkeypatch.setenv("JIRA_PROJECT", "REB")
    fake_client = MagicMock()
    fake_client.update_issue.return_value = {"key": "REB-1", "ok": True}
    with patch.object(applier_mod, "_load_acli", return_value=_make_fake_acli(fake_client)):
        # Must not raise CrossProjectTargetError (other downstream effects are fine).
        try:
            applier_mod.apply([_update("REB-1")], f"reb-ok-{int(time.time())}", repo_root=tmp_path)
        except applier_mod.CrossProjectTargetError as exc:  # pragma: no cover
            pytest.fail(f"guard wrongly blocked a same-project update: {exc!r}")
