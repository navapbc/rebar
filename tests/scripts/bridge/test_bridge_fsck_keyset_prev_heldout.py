"""Held-out oracle for bridge fsck over narrowed key-set snapshots."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _subprocess_env import subprocess_env


@pytest.fixture(scope="module")
def fsck() -> ModuleType:
    from rebar._engine_support import bridge_fsck

    return bridge_fsck


def _write_bindings(tracker: Path, jira_key: str = "REB-464") -> None:
    state = tracker / ".bridge_state"
    state.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "bindings": {"loc-a": {"jira_key": jira_key, "state": "confirmed"}},
        "reverse": {jira_key: "loc-a"},
    }
    (state / "bindings.json").write_text(json.dumps(payload), encoding="utf-8")


def _archived_local() -> list[dict]:
    return [{"ticket_id": "loc-a", "status": "archived", "archived": True}]


@pytest.mark.unit
@pytest.mark.scripts
def test_full_payload_is_unchanged_while_keyset_is_indeterminate(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    _write_bindings(tracker)

    live = fsck.audit_binding_drift(
        tracker,
        local_states=_archived_local(),
        jira_snapshot={"REB-464": {"status": "To Do"}},
    )
    done = fsck.audit_binding_drift(
        tracker,
        local_states=_archived_local(),
        jira_snapshot={"REB-464": {"status": "Done"}},
    )
    keyset = fsck.audit_binding_drift(
        tracker,
        local_states=_archived_local(),
        jira_snapshot={"REB-464": {}},
    )

    assert live["would_terminal"] == [{"local_id": "loc-a", "jira_key": "REB-464"}]
    assert "indeterminate" not in live
    assert done["would_terminal"] == []
    assert "indeterminate" not in done
    assert keyset["would_terminal"] == []
    assert keyset["indeterminate"] == [
        {
            "local_id": "loc-a",
            "jira_key": "REB-464",
            "reason": "jira status unavailable in key-set snapshot",
        }
    ]
    report = fsck._format_report({"binding_drift": keyset})
    assert "indeterminate: local=loc-a jira_key=REB-464" in report
    assert "No issues found." in report


@pytest.mark.unit
@pytest.mark.scripts
def test_keyset_preserves_membership_only_results_and_fences_terminal_status(fsck, tmp_path):
    tracker = tmp_path / ".tickets-tracker"
    state = tracker / ".bridge_state"
    state.mkdir(parents=True)
    bindings = {
        "loc-active": {"jira_key": "REB-1", "state": "confirmed"},
        "loc-gone": {"jira_key": "REB-2", "state": "confirmed"},
        "loc-terminal": {"jira_key": "REB-3", "state": "confirmed"},
    }
    reverse = {entry["jira_key"]: local_id for local_id, entry in bindings.items()}
    (state / "bindings.json").write_text(
        json.dumps({"version": 2, "bindings": bindings, "reverse": reverse}), encoding="utf-8"
    )
    locals_ = [
        {"ticket_id": "loc-active", "status": "in_progress", "archived": False},
        {"ticket_id": "loc-terminal", "status": "archived", "archived": True},
    ]
    full_snapshot = {
        "REB-1": {"status": "In Progress"},
        "REB-2": {"status": "Done"},
        "REB-3": {"status": "Done"},
        "REB-4": {"status": "To Do"},
    }
    keyset_snapshot = {key: {} for key in full_snapshot}

    full = fsck.audit_binding_drift(tracker, local_states=locals_, jira_snapshot=full_snapshot)
    keyset = fsck.audit_binding_drift(tracker, local_states=locals_, jira_snapshot=keyset_snapshot)

    for bucket in ("local_gone", "unbound_jira", "dangling", "retired_overlap"):
        assert keyset[bucket] == full[bucket]
    assert keyset["local_gone"] == [{"local_id": "loc-gone", "jira_key": "REB-2"}]
    assert keyset["unbound_jira"] == [{"jira_key": "REB-4"}]
    assert full["would_terminal"] == []
    assert keyset["would_terminal"] == []
    assert [entry["local_id"] for entry in keyset["indeterminate"]] == ["loc-terminal"]


def _write_event(ticket_dir: Path, timestamp: int, uuid: str, event_type: str, data: dict) -> None:
    payload = {
        "timestamp": timestamp,
        "uuid": uuid,
        "event_type": event_type,
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Test User",
        "data": data,
    }
    (ticket_dir / f"{timestamp}-{uuid}-{event_type}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _cli_tracker(root: Path, snapshot: dict) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    tracker = root / ".tickets-tracker"
    tracker.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "tickets"], cwd=tracker, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tracker, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tracker, check=True)
    ticket_dir = tracker / "loc-a"
    ticket_dir.mkdir()
    _write_event(
        ticket_dir,
        1_742_605_200,
        "11111111-1111-4111-8111-111111111111",
        "CREATE",
        {"ticket_type": "task", "title": "Archived local"},
    )
    _write_event(
        ticket_dir,
        1_742_605_300,
        "22222222-2222-4222-8222-222222222222",
        "ARCHIVED",
        {},
    )
    _write_bindings(tracker)
    (tracker / ".bridge_state" / "prev_snapshot.json").write_text(
        json.dumps(snapshot), encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=tracker, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=tracker, check=True)
    return tracker


def _git_out(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _store_snapshot(root: Path) -> dict[str, object]:
    """Snapshot what "the store did not change" actually means, in two parts.

    Payload: every file OUTSIDE ``.git/`` byte-for-byte -- the event files, ``.bridge_state/``
    and bindings the store owns.

    Git state: the LOGICAL ref state (``HEAD`` plus every ref) rather than the bytes of git's
    object storage. Git's background auto-maintenance rewrites that storage on its own
    schedule -- it takes ``.git/objects/maintenance.lock`` and repacks loose objects into a
    packfile -- so comparing those bytes makes the oracle depend on git's scheduler rather than
    on the command under test. Repacking cannot move a ref, so it is invisible here, while a
    command that actually committed to the store would move ``HEAD`` and be caught.
    """
    payload = {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.name != ".cache.json"
        and path.relative_to(root).parts[0] != ".git"
    }
    return {
        "payload": payload,
        "head": _git_out(root, "rev-parse", "HEAD"),
        "refs": _git_out(root, "for-each-ref", "--format=%(refname) %(objectname)"),
    }


def _run_cli(tracker: Path) -> tuple[subprocess.CompletedProcess[str], dict]:
    env = subprocess_env()
    env["REBAR_ROOT"] = str(tracker.parent)
    env["REBAR_TRACKER_DIR"] = str(tracker)
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "rebar.cli",
            "bridge",
            "fsck",
            "--tickets-tracker",
            str(tracker),
            "--output",
            "json",
        ],
        cwd=tracker.parent,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert cp.stdout, f"bridge fsck emitted no JSON (rc={cp.returncode}, stderr={cp.stderr!r})"
    return cp, json.loads(cp.stdout)


@pytest.mark.unit
@pytest.mark.scripts
def test_cli_keyset_is_informational_and_keeps_json_envelope_read_only(tmp_path):
    full_tracker = _cli_tracker(tmp_path / "full", {"REB-464": {"status": "To Do"}})
    keyset_tracker = _cli_tracker(tmp_path / "keyset", {"REB-464": {}})
    full_before = _store_snapshot(full_tracker)
    keyset_before = _store_snapshot(keyset_tracker)

    full_cp, full = _run_cli(full_tracker)
    keyset_cp, keyset = _run_cli(keyset_tracker)

    assert full_cp.returncode == 1, full_cp.stderr
    assert keyset_cp.returncode == 0, keyset_cp.stderr
    assert (
        set(keyset)
        == set(full)
        == {
            "unknown_event_types",
            "binding_drift",
            "store_integrity",
        }
    )
    assert full["store_integrity"] == keyset["store_integrity"] == []
    assert full["binding_drift"]["would_terminal"] == [{"local_id": "loc-a", "jira_key": "REB-464"}]
    assert keyset["binding_drift"]["would_terminal"] == []
    assert keyset["binding_drift"]["indeterminate"][0]["local_id"] == "loc-a"
    assert _store_snapshot(full_tracker) == full_before
    assert _store_snapshot(keyset_tracker) == keyset_before


@pytest.mark.unit
@pytest.mark.scripts
def test_store_snapshot_survives_git_maintenance_but_still_catches_mutation(tmp_path):
    """The read-only oracle must answer "did the store change?", not "did git reorganise?".

    Git auto-maintenance runs on its own schedule: it takes a lock under ``.git/objects/`` and
    repacks loose objects. Neither is a store mutation, and both used to flip a byte-for-byte
    snapshot of the whole tracker directory -- which is how this suite went intermittently red.
    Both limbs are exercised here, and the second half of the test proves the oracle did not buy
    that stability by going blind.
    """
    tracker = _cli_tracker(tmp_path / "churn", {"REB-464": {"status": "To Do"}})
    before = _store_snapshot(tracker)

    lock = tracker / ".git" / "objects" / "maintenance.lock"
    lock.write_bytes(b"")
    assert _store_snapshot(tracker) == before, "a maintenance lock file is not a store mutation"
    lock.unlink()

    subprocess.run(["git", "gc", "--quiet"], cwd=tracker, check=True)
    assert _store_snapshot(tracker) == before, "repacking objects is not a store mutation"

    payload_file = tracker / ".bridge_state" / "prev_snapshot.json"
    payload_file.write_text(json.dumps({"REB-464": {"status": "Done"}}), encoding="utf-8")
    mutated = _store_snapshot(tracker)
    assert mutated != before, "a rewritten store payload file must be detected"

    subprocess.run(["git", "add", "."], cwd=tracker, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "mutation"], cwd=tracker, check=True)
    committed = _store_snapshot(tracker)
    assert committed["head"] != before["head"], "a commit that moves HEAD must be detected"
    assert committed["refs"] != before["refs"], "a commit that moves refs must be detected"
