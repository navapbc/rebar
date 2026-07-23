"""Ticket 97f2: ``apply_handlers`` soft-fails on the NEUTRAL assignee error base.

The pre-97f2 code caught the vendor ``adapters.jira.acli_subprocess.AssigneeNotFoundError``
by a direct import. 97f2 routes the catch through the neutral
``rebar_reconciler._backend.BackendAssigneeNotFoundError`` base instead, so the core
module carries no ``adapters.jira`` import.

This is the teeth for "catches the NEUTRAL base": we raise the base type itself (not the
Jira subclass) from the transport. If ``apply_handlers`` reverted to catching only the
vendor subclass, the base would escape and kill the batch — this test would fail.
Companion to ``test_applier_assignee_soft_fail.py`` (which raises the vendor subclass).
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

# Mirror the sibling soft-fail test's bootstrap so the loader chain resolves the
# ``rebar_reconciler`` namespace to the real source dir under any cwd — this makes the
# ``BackendAssigneeNotFoundError`` we import below the SAME class object apply_handlers
# catches.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if "rebar_reconciler" not in sys.modules:
    import types as _types

    _dr = _types.ModuleType("rebar_reconciler")
    _dr.__path__ = [str(SCRIPTS_DIR / "rebar_reconciler")]
    sys.modules["rebar_reconciler"] = _dr
for _name, _rel in (
    ("rebar_reconciler.adapters.jira.adf", "adapters/jira/adf.py"),
    ("rebar_reconciler.adapters.jira.comment_limits", "adapters/jira/comment_limits.py"),
):
    if _name not in sys.modules:
        _spec = importlib.util.spec_from_file_location(
            _name, SCRIPTS_DIR / "rebar_reconciler" / _rel
        )
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[_name] = _mod
        _spec.loader.exec_module(_mod)  # type: ignore[union-attr]

from rebar_reconciler._backend import BackendAssigneeNotFoundError  # noqa: E402


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier_mod() -> ModuleType:
    return _load_module("applier_neutral_assignee", APPLIER_PATH)


def _read_alert_records(repo_root: Path) -> list[dict]:
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
