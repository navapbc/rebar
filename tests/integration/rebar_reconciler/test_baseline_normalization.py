"""A3 held-out integration oracle over the real reconcile baseline-advance seam."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_ADF_DESCRIPTION = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "persisted body"}],
        }
    ],
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_real_pass_persists_scalars_and_equivalent_second_pass_is_byte_stable(
    tmp_path: Path,
) -> None:
    from rebar_reconciler.binding_store import BindingStore
    from rebar_reconciler.reconcile import _advance_baselines

    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _git(tracker, "init", "-q")
    _git(tracker, "config", "user.name", "A3 Oracle")
    _git(tracker, "config", "user.email", "a3-oracle@example.test")

    store = BindingStore(tracker)
    store.bind_confirm("loc-1", "REB-1")
    store.save()
    _git(tracker, "add", ".")
    _git(tracker, "commit", "-qm", "initialize binding")

    assignee = {
        "displayName": "Ada Lovelace",
        "emailAddress": "ada@example.test",
        "accountId": "acct-ada",
    }
    vendor_snapshot = {
        "REB-1": {
            "summary": "Summary",
            "description": _ADF_DESCRIPTION,
            "priority": {"id": "1", "name": "High"},
            "status": {"id": "3", "name": "In Progress"},
            "assignee": assignee,
        }
    }
    assert _advance_baselines(store, vendor_snapshot) == 1
    store.save()

    bindings_path = tracker / ".bridge_state" / "bindings.json"
    first_bytes = bindings_path.read_bytes()
    persisted = json.loads(first_bytes)["bindings"]["loc-1"]["baseline"]
    assert persisted == {
        "summary": "Summary",
        "description": "persisted body",
        "priority": "High",
        "status": "In Progress",
        "assignee": assignee,
    }

    _git(tracker, "add", ".")
    _git(tracker, "commit", "-qm", "persist normalized baseline")
    scalar_snapshot = {
        "REB-1": {
            "summary": "Summary",
            "description": "persisted body",
            "priority": "High",
            "status": "In Progress",
            "assignee": assignee,
        }
    }
    assert _advance_baselines(store, scalar_snapshot) == 1
    store.save()

    assert bindings_path.read_bytes() == first_bytes
    assert _git(tracker, "status", "--porcelain") == ""
