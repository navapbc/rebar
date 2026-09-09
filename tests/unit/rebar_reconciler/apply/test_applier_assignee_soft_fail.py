"""Verify assignee resolution failures remain local to one update.

`AssigneeNotFoundError` records a `bridge_alerts/<date>.jsonl` entry and
lets valid sibling mutations run. The batch returns without discarding
independent writes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR / "rebar_reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR / "rebar_reconciler" / "adapters" / "jira" / "acli.py"
ALERT_STORE_PATH = SCRIPTS_DIR / "rebar_reconciler" / "alert_store.py"

# acli.py imports ``from rebar_reconciler.adapters.jira.adf import text_to_adf``,
# which requires the rebar_reconciler package to be importable. Mirror the
# bootstrap pattern from test_assignee_validation.py so the loader chain
# resolves the same way under any cwd.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
_ADF_PATH = SCRIPTS_DIR / "rebar_reconciler" / "adapters" / "jira" / "adf.py"
if "rebar_reconciler" not in sys.modules:
    import types as _types

    _dr = _types.ModuleType("rebar_reconciler")
    _dr.__path__ = [str(SCRIPTS_DIR / "rebar_reconciler")]
    sys.modules["rebar_reconciler"] = _dr
if "rebar_reconciler.adapters.jira.adf" not in sys.modules:
    _adf_spec = importlib.util.spec_from_file_location(
        "rebar_reconciler.adapters.jira.adf", _ADF_PATH
    )
    _adf_mod = importlib.util.module_from_spec(_adf_spec)
    sys.modules["rebar_reconciler.adapters.jira.adf"] = _adf_mod
    _adf_spec.loader.exec_module(_adf_mod)  # type: ignore[union-attr]
# acli.py also imports ``from rebar_reconciler.adapters.jira.comment_limits import ...``
# (bug 6afc-20ee-84e5-4dd5). Bootstrap it explicitly alongside adf so the loader
# chain resolves regardless of which sibling test first registered the
# ``rebar_reconciler`` namespace stub.
_CL_PATH = SCRIPTS_DIR / "rebar_reconciler" / "adapters" / "jira" / "comment_limits.py"
if "rebar_reconciler.adapters.jira.comment_limits" not in sys.modules:
    _cl_spec = importlib.util.spec_from_file_location(
        "rebar_reconciler.adapters.jira.comment_limits", _CL_PATH
    )
    _cl_mod = importlib.util.module_from_spec(_cl_spec)
    sys.modules["rebar_reconciler.adapters.jira.comment_limits"] = _cl_mod
    _cl_spec.loader.exec_module(_cl_mod)  # type: ignore[union-attr]


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier_mod() -> ModuleType:
    return _load_module("applier_assignee_softfail", APPLIER_PATH)


@pytest.fixture(scope="module")
def acli_mod() -> ModuleType:
    return _load_module("acli_assignee_softfail", ACLI_PATH)


@pytest.fixture(scope="module")
def alert_store_mod() -> ModuleType:
    return _load_module("alert_store_assignee_softfail", ALERT_STORE_PATH)


def _read_alert_records(repo_root: Path) -> list[dict]:
    # alert_store writes to <repo_root>/bridge_state/bridge_alerts/<date>.jsonl
    alerts_dir = repo_root / "bridge_state" / "bridge_alerts"
    if not alerts_dir.is_dir():
        return []
    out = []
    for jf in sorted(alerts_dir.glob("*.jsonl")):
        for line in jf.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def test_assignee_not_found_soft_fails_batch_continues(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
) -> None:
    """Record the bad assignee and continue to the valid sibling update."""
    pass_id = f"test-pass-{int(time.time())}"

    good_mutation = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-4275",
        "fields": {"summary": "still works"},
        "local_id": "good-local-id",
    }
    bad_mutation = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-4276",
        "fields": {"assignee": "Worktree"},  # the prod-failure assignee
        "local_id": "bad-local-id",
    }

    # _apply_batch constructs its own AcliClient via _load_acli();
    # patch the loader so it returns a fake module whose AcliClient
    # constructor returns our mock. This is the same boundary every
    # other applier test uses to override the live ACLI layer.
    fake_client = MagicMock()

    def _update_issue_side_effect(issue_key, **kwargs):
        if issue_key == "DIG-4276":
            raise acli_mod.AssigneeNotFoundError(
                "validate_assignee_exists: no assignable user matches "
                "'Worktree' for issue='DIG-4276'"
            )
        return {"key": issue_key, "ok": True}

    fake_client.update_issue.side_effect = _update_issue_side_effect
    # S4: _load_acli returns the transport DIRECTLY. apply_handlers catches
    # AssigneeNotFoundError via a direct import from rebar_reconciler.adapters.jira.acli_subprocess,
    # which — because the shadow ``rebar_reconciler`` package __path__'s to the real
    # source dir — is the SAME class object our side_effect raises.
    with patch.object(applier_mod, "_load_acli", return_value=fake_client):
        try:
            applier_mod.apply(
                [good_mutation, bad_mutation],
                pass_id,
                repo_root=tmp_path,
            )
        except acli_mod.AssigneeNotFoundError as exc:
            pytest.fail(
                f"applier.apply propagated AssigneeNotFoundError instead "
                f"of soft-failing the batch: {exc!r}"
            )

    # Valid update DID run
    update_calls = list(fake_client.update_issue.call_args_list)
    assert len(update_calls) == 2, (
        f"both mutations should have been attempted; got {len(update_calls)} calls"
    )
    # Alert record DID land for the bad one
    records = _read_alert_records(tmp_path)
    assignee_alerts = [
        r for r in records if "assignee" in r.get("kind", "") and r.get("key") == "DIG-4276"
    ]
    assert len(assignee_alerts) >= 1, (
        f"expected an assignee-unresolved alert for DIG-4276; got records: {records}"
    )


def test_assignee_not_found_alone_does_not_raise(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
) -> None:
    """Even when the bad mutation is the ONLY one in the batch,
    apply() must not raise — it should record-and-continue (returning
    an empty/partial result rather than aborting).
    """
    bad_mutation = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-4276",
        "fields": {"assignee": "Worktree"},
        "local_id": "bad-local-id",
    }
    fake_client = MagicMock()
    fake_client.update_issue.side_effect = acli_mod.AssigneeNotFoundError(
        "validate_assignee_exists: no assignable user matches 'Worktree'"
    )
    # S4: _load_acli returns the transport directly.
    with patch.object(applier_mod, "_load_acli", return_value=fake_client):
        try:
            applier_mod.apply(
                [bad_mutation],
                f"test-pass-solo-{int(time.time())}",
                repo_root=tmp_path,
            )
        except acli_mod.AssigneeNotFoundError as exc:
            pytest.fail(f"single-mutation batch with bad assignee must not raise: {exc!r}")

    records = _read_alert_records(tmp_path)
    assert any(r.get("key") == "DIG-4276" and "assignee" in r.get("kind", "") for r in records), (
        f"expected assignee alert; got {records}"
    )
