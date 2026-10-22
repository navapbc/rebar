"""Verify assignee failures use the backend-neutral exception boundary.

The transport raises `BackendAssigneeNotFoundError` directly, proving
`apply_handlers` does not depend on a Jira subclass. The core retains no
`adapters.jira` import. The vendor-subclass case remains in
`test_applier_assignee_soft_fail.py`.
"""

from __future__ import annotations

import importlib
import json
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from rebar.reconciler import scratch_retention
from rebar.reconciler._backend import BackendAssigneeNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR.parent / "reconciler" / "applier.py"


@pytest.fixture(scope="module")
def applier_mod() -> ModuleType:
    return importlib.import_module("rebar.reconciler.applier")


def _read_alert_records(repo_root: Path) -> list[dict]:
    alerts_dir = scratch_retention.alerts_dir(repo_root)
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


def test_neutral_assignee_error_soft_fails(applier_mod: ModuleType, tmp_path: Path) -> None:
    """Raising the NEUTRAL ``BackendAssigneeNotFoundError`` (not the Jira subclass) from
    the transport must be soft-failed by apply_handlers: apply() returns without raising
    and an assignee alert is recorded."""
    bad_mutation = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-4276",
        "fields": {"assignee": "Worktree"},
        "local_id": "bad-local-id",
    }
    fake_client = MagicMock()
    fake_client.update_issue.side_effect = BackendAssigneeNotFoundError(
        "no assignable user matches 'Worktree' for issue='DIG-4276'"
    )
    with patch.object(applier_mod, "_load_acli", return_value=fake_client):
        try:
            applier_mod.apply(
                [bad_mutation],
                f"test-pass-neutral-{int(time.time())}",
                repo_root=tmp_path,
            )
        except BackendAssigneeNotFoundError as exc:
            pytest.fail(
                f"apply() propagated the neutral BackendAssigneeNotFoundError instead of "
                f"soft-failing — apply_handlers must catch the neutral base: {exc!r}"
            )

    records = _read_alert_records(tmp_path)
    assert any(r.get("key") == "DIG-4276" and "assignee" in r.get("kind", "") for r in records), (
        f"expected an assignee-unresolved alert for the neutral-base raise; got {records}"
    )
