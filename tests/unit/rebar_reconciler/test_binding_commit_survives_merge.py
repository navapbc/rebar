"""Regression test: binding-store snapshot committed to tickets branch after reconciler pass.

Probe TS 1780561673 root cause: ``binding_store.save()`` writes bindings.json only
to the working-tree filesystem.  Between reconciler passes, the ticket-CLI's
``_push_tickets_branch()`` may run ``git merge origin/tickets`` which overwrites
the un-committed local bindings.json with the version from the remote — losing the
Phase-1 bindings and causing Phase-2 to generate outbound CREATE mutations (dedup-
skip) instead of UPDATE mutations with the edited field values.

Fix: ``reconcile_once`` calls ``_commit_binding_store_snapshot`` after every
``binding_store.save()``, staging and committing ``.bridge_state/bindings.json``
to the tickets orphan branch.  Subsequent ``git merge origin/tickets`` calls in
the ticket-CLI include the new bindings because they are already on-branch.

RED test (before fix): after a reconciler pass that creates bindings, the bindings
    are present on-disk but NOT committed to the tickets branch. Simulating a
    ``git merge origin/tickets`` that restores the old bindings.json leaves the
    binding store empty → the next reconciler pass sees the ticket as unbound →
    generates a CREATE mutation instead of an UPDATE.

GREEN test (after fix): ``_commit_binding_store_snapshot`` commits bindings.json
    to the tickets branch.  Even after a simulated merge that would otherwise
    overwrite the file, the bindings survive because they are now on-branch.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"
BINDING_STORE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "binding_store.py"
OUTBOUND_DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"


def _load_module(name: str, path: Path):
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def reconcile_mod():
    mod = _load_module("_test_reconcile_bcs", RECONCILE_PATH)
    yield mod
    sys.modules.pop("_test_reconcile_bcs", None)


@pytest.fixture
def binding_store_mod():
    mod = _load_module("_test_bcs_binding_store", BINDING_STORE_PATH)
    yield mod
    sys.modules.pop("_test_bcs_binding_store", None)


@pytest.fixture
def outbound_differ_mod():
    # Install a stub ADF module under a unique key that only _this_ module's
    # outbound_differ copy will see.  We must NOT clobber the canonical
    # "rebar_reconciler.adf" key that other test modules rely on.
    _ADF_KEY = "rebar_reconciler.adf"
    _prev_adf = sys.modules.get(_ADF_KEY)
    adf_stub = types.ModuleType(_ADF_KEY)
    adf_stub.adf_to_text = lambda x: str(x) if isinstance(x, str) else ""
    sys.modules[_ADF_KEY] = adf_stub
    mod = _load_module("_test_bcs_outbound_differ", OUTBOUND_DIFFER_PATH)
    yield mod
    # Teardown: restore the original ADF module (or remove if it wasn't there).
    if _prev_adf is None:
        sys.modules.pop(_ADF_KEY, None)
    else:
        sys.modules[_ADF_KEY] = _prev_adf
    # Also clean up the outbound differ module so it doesn't share the stub.
    sys.modules.pop("_test_bcs_outbound_differ", None)


def _init_tickets_git_repo(tracker_dir: Path) -> None:
    """Initialise tracker_dir as a bare git repo on an orphan 'tickets' branch.

    Creates a minimal initial commit so that subsequent ``git add + commit``
    calls have a valid parent.
    """
    tracker_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "tickets", str(tracker_dir)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tracker_dir), "config", "user.email", "test@test.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tracker_dir), "config", "user.name", "Test"],
                   check=True, capture_output=True)
    # Create an initial empty commit so the branch exists
    subprocess.run(["git", "-C", str(tracker_dir), "commit",
                    "--allow-empty", "-m", "init", "--no-verify"],
                   check=True, capture_output=True)


def _binding_in_tickets_head(tracker_dir: Path, local_id: str, jira_key: str) -> bool:
    """Return True if the tickets branch HEAD's bindings.json contains local_id→jira_key."""
    result = subprocess.run(
        ["git", "-C", str(tracker_dir), "show", "HEAD:.bridge_state/bindings.json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    entry = data.get("bindings", {}).get(local_id)
    if entry is None:
        return False
    return entry.get("jira_key") == jira_key and entry.get("state") == "confirmed"


def test_binding_store_committed_to_tickets_branch_after_reconcile_pass(
    tmp_path, reconcile_mod, binding_store_mod, outbound_differ_mod,
):
    """After reconcile_once saves a new binding, bindings.json is committed to the
    tickets orphan branch.

    Without the fix, ``binding_store.save()`` only writes the working-tree file.
    A subsequent ``git merge origin/tickets`` can overwrite it with the remote
    version (which lacks the new binding), causing the next pass to treat the
    ticket as unbound.

    This test verifies that after reconcile_once, the binding IS on the tickets
    branch (committed), so merges include it.
    """
    # Set up the tickets orphan branch worktree so git operations work.
    tracker_dir = tmp_path / ".tickets-tracker"
    _init_tickets_git_repo(tracker_dir)

    # Create initial bindings.json on the tickets branch (simulating the
    # "remote" state with no bindings for our probe ticket).
    bridge_dir = tracker_dir / ".bridge_state"
    bridge_dir.mkdir(parents=True, exist_ok=True)
    bindings_path = bridge_dir / "bindings.json"
    empty_bindings = {"bindings": {}, "reverse": {}}
    bindings_path.write_text(json.dumps(empty_bindings))

    # Commit the empty bindings.json so it's on the tickets branch.
    subprocess.run(
        ["git", "-C", str(tracker_dir), "add", ".bridge_state/bindings.json"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tracker_dir), "commit", "--no-verify",
         "-m", "init bindings"],
        check=True, capture_output=True,
    )

    # Add a confirmed binding (simulating Phase-1 reconciler creating a Jira issue).
    bs = binding_store_mod.load_binding_store(tmp_path)
    bs.bind_confirm("probe-ticket-1", "DIG-5999")
    bs.save()

    # PRE-FIX state: bindings.json on disk has the new binding, but the tickets
    # branch HEAD still carries the EMPTY version (empty_bindings committed above).
    assert not _binding_in_tickets_head(tracker_dir, "probe-ticket-1", "DIG-5999"), (
        "PRE-FIX: tickets branch HEAD must NOT yet contain the new binding "
        "(only saved to filesystem); if this fails the test setup is wrong."
    )

    # Demonstrate the regression: 'git merge origin/tickets' (simulated by
    # restoring from HEAD) overwrites the working-tree file with the empty version.
    subprocess.run(
        ["git", "-C", str(tracker_dir), "checkout", "HEAD", "--",
         ".bridge_state/bindings.json"],
        check=True, capture_output=True,
    )
    reloaded = binding_store_mod.load_binding_store(tmp_path)
    assert reloaded.get_jira_key("probe-ticket-1") is None, (
        "After simulated merge (restoring HEAD), binding must be lost. "
        "This confirms the regression: the binding is missing for the next pass."
    )

    # Apply the fix: re-save the binding and call _commit_binding_store_snapshot.
    bs2 = binding_store_mod.load_binding_store(tmp_path)
    bs2.bind_confirm("probe-ticket-1", "DIG-5999")
    bs2.save()

    reconcile_mod._commit_binding_store_snapshot(bs2, tmp_path, "test-pass-001")

    # POST-FIX: tickets branch HEAD now contains the new binding.
    assert _binding_in_tickets_head(tracker_dir, "probe-ticket-1", "DIG-5999"), (
        "POST-FIX: _commit_binding_store_snapshot must commit bindings.json to "
        "the tickets orphan branch HEAD. The binding probe-ticket-1 → DIG-5999 "
        "must be present in the committed version so that 'git merge origin/tickets' "
        "or 'git checkout HEAD -- .bridge_state/bindings.json' preserves it."
    )

    # Verify end-to-end: even if the working-tree file is overwritten (merge
    # scenario), restoring from the tickets branch HEAD gives back the new binding.
    bindings_path.write_text(json.dumps(empty_bindings))
    subprocess.run(
        ["git", "-C", str(tracker_dir), "checkout", "HEAD", "--",
         ".bridge_state/bindings.json"],
        check=True, capture_output=True,
    )
    restored = binding_store_mod.load_binding_store(tmp_path)
    assert restored.get_jira_key("probe-ticket-1") == "DIG-5999", (
        "After restoring bindings.json from the committed tickets branch HEAD, "
        "the binding probe-ticket-1 → DIG-5999 must be present. "
        "This is the end-to-end fix: the committed version carries the new binding."
    )


def test_outbound_update_generated_when_bindings_present(
    tmp_path, outbound_differ_mod, binding_store_mod,
):
    """Integration seam test: compute_outbound_mutations generates UPDATE (not CREATE)
    for a bound ticket with changed scalar fields.

    Exercises the reconcile.py → compute_outbound_mutations call path with real
    binding_store signatures.  This is the integration seam the probe exercises in
    Phase 2.

    Root-cause evidence (probe TS 1780561673): Phase 2 showed
      filter: 320 mutations computed, 10 match filter (10 local IDs, 10 target keys)
    where '10 target keys == 10 local IDs' proves zero Jira keys were in the binding
    store — every mutation was a CREATE, none were UPDATEs.
    """
    # Set up binding store with probe-ticket-1 → DIG-5999 (confirmed, as if Phase 1 ran)
    tracker_dir = tmp_path / ".tickets-tracker"
    bridge_dir = tracker_dir / ".bridge_state"
    bridge_dir.mkdir(parents=True)
    bindings_data = {
        "bindings": {
            "probe-ticket-1": {
                "jira_key": "DIG-5999",
                "state": "confirmed",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        },
        "reverse": {"DIG-5999": "probe-ticket-1"},
    }
    (bridge_dir / "bindings.json").write_text(json.dumps(bindings_data))

    bs = binding_store_mod.load_binding_store(tmp_path)
    assert bs.get_jira_key("probe-ticket-1") == "DIG-5999", (
        "Test setup: binding must be confirmed before calling compute_outbound_mutations"
    )

    # Local ticket with CHANGED fields (Phase 2 state: after local edits)
    local_ticket = {
        "ticket_id": "probe-ticket-1",
        "title": "UPDATED TITLE — Phase 2 edit",
        "description": "Updated description",
        "status": "open",
        "priority": 3,   # Changed from 0 (Highest) to 3 (Low)
        "ticket_type": "task",
        "assignee": "",
        "tags": ["field-probe-test"],
        "comments": [],
    }

    # Jira snapshot with CREATE-time (Phase 1) field values — what Jira currently has
    jira_snapshot = {
        "DIG-5999": {
            "summary": "FIELD-PROBE-1: title baseline",
            "description": "Baseline description for probe",
            "priority": {"name": "Highest"},
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "assignee": None,
            "labels": ["field-probe-test", "dso-id:probe-ticket-1"],
        }
    }

    mutations = outbound_differ_mod.compute_outbound_mutations(
        [local_ticket],
        jira_snapshot,
        bs,
    )

    assert len(mutations) == 1, (
        f"Expected exactly 1 mutation, got {len(mutations)}. "
        f"Actions: {[m.action for m in mutations]}"
    )
    m = mutations[0]
    assert m.action == "update", (
        f"Expected UPDATE mutation (bound ticket with changed fields), got action={m.action!r}. "
        f"This confirms the regression: with bindings present, compute_outbound_mutations "
        f"must generate UPDATE not CREATE. When bindings are MISSING (lost via git merge), "
        f"the action would be 'create' — exactly the probe TS 1780561673 symptom."
    )
    assert m.jira_key == "DIG-5999", (
        f"UPDATE mutation must target the Jira key DIG-5999, got {m.jira_key!r}"
    )
    # Verify the changed scalar fields are included
    assert "summary" in m.fields, (
        f"UPDATE mutation must include 'summary' in changed_fields. Got: {m.fields}"
    )
    assert "priority" in m.fields, (
        f"UPDATE mutation must include 'priority' in changed_fields (0→Low). Got: {m.fields}"
    )
    assert m.fields["priority"] == "Low", (
        f"Priority must map to 'Low' (local priority=3). Got: {m.fields['priority']!r}"
    )
