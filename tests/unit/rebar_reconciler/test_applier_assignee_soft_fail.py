"""Bug 17b5-dda4-6662-4616: AssigneeNotFoundError must soft-fail.

Production cron evidence (GHA run 26657962362, 2026-05-29T19:34:53Z):

    RECON: batch_outcome action=update key=DIG-4275 error=None
    RECON: batch_outcome action=update key=DIG-4276 error=None
    ERROR: reconcile_once raised: validate_assignee_exists:
      no assignable user matches 'Worktree' for issue='DIG-4276'
    ##[error]Process completed with exit code 1.

The Phase A client-side assignee validator (commit 84a3aab72c) raises
``AssigneeNotFoundError`` (a ValueError subclass) when the assignee
doesn't map to a real Jira account. The applier batch loop in
``applier.apply`` (applier.py:2782-2882) only catches ``HeadDriftError``;
``AssigneeNotFoundError`` escapes and kills the entire pass.

Fix: per-mutation catch in the applier outbound batch loop that records
a ``bridge_alerts`` JSONL entry and continues. Mirrors the existing
soft-fail patterns (create-identity BRIDGE_ALERT, 400-illegal-transition
comment fallback).

RED test: dispatch two outbound update mutations through ``apply()``.
One has a valid assignee; one triggers ``AssigneeNotFoundError``. Without
the fix, ``apply()`` raises and the good one never runs. With the fix,
``apply()`` returns successfully, the good mutation applies, and an
alert record lands in ``bridge_alerts/<date>.jsonl``.
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

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR / "rebar_reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR / "acli-integration.py"
ALERT_STORE_PATH = SCRIPTS_DIR / "rebar_reconciler" / "alert_store.py"

# acli-integration.py imports ``from rebar_reconciler.adf import text_to_adf``,
# which requires the rebar_reconciler package to be importable. Mirror the
# bootstrap pattern from test_assignee_validation.py so the loader chain
# resolves the same way under any cwd.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
_ADF_PATH = SCRIPTS_DIR / "rebar_reconciler" / "adf.py"
if "rebar_reconciler" not in sys.modules:
    import types as _types

    _dr = _types.ModuleType("rebar_reconciler")
    _dr.__path__ = [str(SCRIPTS_DIR / "rebar_reconciler")]
    sys.modules["rebar_reconciler"] = _dr
if "rebar_reconciler.adf" not in sys.modules:
    _adf_spec = importlib.util.spec_from_file_location("rebar_reconciler.adf", _ADF_PATH)
    _adf_mod = importlib.util.module_from_spec(_adf_spec)
    sys.modules["rebar_reconciler.adf"] = _adf_mod
    _adf_spec.loader.exec_module(_adf_mod)  # type: ignore[union-attr]
# acli-integration.py also imports ``from rebar_reconciler.comment_limits import ...``
# (bug 6afc-20ee-84e5-4dd5). Bootstrap it explicitly alongside adf so the loader
# chain resolves regardless of which sibling test first registered the
# ``rebar_reconciler`` namespace stub.
_CL_PATH = SCRIPTS_DIR / "rebar_reconciler" / "comment_limits.py"
if "rebar_reconciler.comment_limits" not in sys.modules:
    _cl_spec = importlib.util.spec_from_file_location(
        "rebar_reconciler.comment_limits", _CL_PATH
    )
    _cl_mod = importlib.util.module_from_spec(_cl_spec)
    sys.modules["rebar_reconciler.comment_limits"] = _cl_mod
    _cl_spec.loader.exec_module(_cl_mod)  # type: ignore[union-attr]


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
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
    """The exact production scenario: 1 valid update + 1 bad-assignee
    update in the same batch.

    Pre-fix: AssigneeNotFoundError raised by client.update_issue
    propagates through applier.apply, killing the whole pass — the
    valid mutation never gets a chance to run.

    Post-fix: applier catches AssigneeNotFoundError per-mutation,
    records a bridge_alerts entry, and continues. The valid mutation
    applies; apply() returns without raising.
    """
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
    fake_acli_mod = MagicMock()
    fake_acli_mod.AcliClient.return_value = fake_client
    # Expose the real AssigneeNotFoundError on the fake module so any
    # applier-side ``except _load_acli().AssigneeNotFoundError`` resolves
    # to the same class instances our side_effect raises.
    fake_acli_mod.AssigneeNotFoundError = acli_mod.AssigneeNotFoundError

    with patch.object(applier_mod, "_load_acli", return_value=fake_acli_mod):
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
        r
        for r in records
        if "assignee" in r.get("kind", "") and r.get("key") == "DIG-4276"
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
    fake_acli_mod = MagicMock()
    fake_acli_mod.AcliClient.return_value = fake_client
    fake_acli_mod.AssigneeNotFoundError = acli_mod.AssigneeNotFoundError

    with patch.object(applier_mod, "_load_acli", return_value=fake_acli_mod):
        try:
            applier_mod.apply(
                [bad_mutation],
                f"test-pass-solo-{int(time.time())}",
                repo_root=tmp_path,
            )
        except acli_mod.AssigneeNotFoundError as exc:
            pytest.fail(
                f"single-mutation batch with bad assignee must not raise: {exc!r}"
            )

    records = _read_alert_records(tmp_path)
    assert any(
        r.get("key") == "DIG-4276" and "assignee" in r.get("kind", "") for r in records
    ), f"expected assignee alert; got {records}"
