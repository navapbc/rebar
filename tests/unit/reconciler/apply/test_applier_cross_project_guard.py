"""Bug 626d: cross-project safety guard on the outbound apply path.

A store carrying stale bindings/labels for a different Jira project must NOT push
outbound updates/deletes at that project's issues. ``_apply_batch`` performs a
pre-flight scan (before any Jira write) and raises ``CrossProjectTargetError`` —
fail-closed — when any outbound update/delete targets a project other than the
configured ``jira.project``. Creates (local-id placeholder key) and inbound
mutations are exempt.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from rebar.reconciler.mutation import Mutation, MutationAction, MutationDirection
from rebar.reconciler.mutation_payloads import OutboundUpdatePayload

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR.parent / "reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "acli.py"

# Bootstrap the rebar.reconciler package namespace + the submodules acli.py imports
# at load time, so the importlib loader chain resolves under any cwd (mirrors the
# sibling applier tests).


@pytest.fixture(scope="module")
def applier_mod() -> Iterator[ModuleType]:
    name = "applier_cross_project_guard"
    mod = importlib.import_module("rebar.reconciler.applier")
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def acli_mod() -> Iterator[ModuleType]:
    name = "acli_cross_project_guard"
    mod = importlib.import_module("rebar.reconciler.adapters.jira.acli")
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


def _make_fake_acli(client: MagicMock) -> MagicMock:
    # S4: _load_acli returns the transport directly.
    return client


def _update(key: str, local_id: str = "loc") -> dict:
    """A batch dict, the shape the pure ``_cross_project_targets`` helper scans."""
    return {
        "direction": "outbound",
        "action": "update",
        "key": key,
        "fields": {"summary": "x"},
        "local_id": local_id,
    }


def _typed_update(key: str, local_id: str = "loc") -> Mutation:
    """The typed outbound update ``apply()``'s batch form takes."""
    return Mutation(
        MutationDirection.outbound,
        MutationAction.update,
        key,
        OutboundUpdatePayload(changed_fields={"summary": "x"}, local_id=local_id),
        {},
    )


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
            applier_mod.apply(
                [_typed_update("DIG-100")], f"xproj-{int(time.time())}", repo_root=tmp_path
            )
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
            applier_mod.apply(
                [_typed_update("REB-1")], f"reb-ok-{int(time.time())}", repo_root=tmp_path
            )
        except applier_mod.CrossProjectTargetError as exc:  # pragma: no cover
            pytest.fail(f"guard wrongly blocked a same-project update: {exc!r}")


# --- Bug 7b9a finding 2: the guard's store-loading wiring (many-to-many) -----
# The single-project tests above pin the fallback (_project). These drive the
# real read_projects(repo_root) wiring in apply(): a SEEDED projects.json widens
# the allowed scope to the whole project SET, exercised end-to-end through the
# caller (not the pure _cross_project_targets helper).


def _seed_projects_json(repo_root: Path, keys: list[str]) -> None:
    import json

    bridge = repo_root / ".tickets-tracker" / ".bridge_state"
    bridge.mkdir(parents=True, exist_ok=True)
    record = {
        "version": 1,
        "legacy_default": keys[0],
        "projects": {k: {"repos": []} for k in keys},
    }
    (bridge / "projects.json").write_text(json.dumps(record), encoding="utf-8")


def test_apply_allows_a_second_seeded_project_via_store_loading(
    applier_mod, acli_mod, tmp_path, monkeypatch
):
    """With JIRA_PROJECT=REB but a seeded mapping {REB, DIG}, an update at DIG-1 is
    ALLOWED — proving apply() loads read_projects(repo_root) and widens the scope
    beyond the single _project. Under the single-project fallback DIG-1 would be
    refused (see test_apply_refuses_foreign_project_before_any_write)."""
    monkeypatch.setenv("JIRA_PROJECT", "REB")
    _seed_projects_json(tmp_path, ["REB", "DIG"])
    fake_client = MagicMock()
    fake_client.update_issue.return_value = {"key": "DIG-1", "ok": True}
    with patch.object(applier_mod, "_load_acli", return_value=_make_fake_acli(fake_client)):
        try:
            applier_mod.apply(
                [_typed_update("DIG-1")], f"seeded-ok-{int(time.time())}", repo_root=tmp_path
            )
        except applier_mod.CrossProjectTargetError as exc:  # pragma: no cover
            pytest.fail(
                f"seeded mapping {{REB, DIG}} must allow a DIG-1 update through the "
                f"store-loading wiring; got {exc!r}"
            )


def test_apply_refuses_a_project_outside_the_seeded_set(
    applier_mod, acli_mod, tmp_path, monkeypatch
):
    """A key outside the SEEDED set is still refused before any write — the widened
    scope is the mapping's set, not 'anything'."""
    monkeypatch.setenv("JIRA_PROJECT", "REB")
    _seed_projects_json(tmp_path, ["REB", "DIG"])
    fake_client = MagicMock()
    with patch.object(applier_mod, "_load_acli", return_value=_make_fake_acli(fake_client)):
        with pytest.raises(applier_mod.CrossProjectTargetError) as exc:
            applier_mod.apply(
                [_typed_update("ACME-1")], f"seeded-bad-{int(time.time())}", repo_root=tmp_path
            )
    assert "ACME-1" in str(exc.value)
    fake_client.update_issue.assert_not_called()
