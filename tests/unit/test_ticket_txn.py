"""WS2 in-process unit tests for the extracted transition critical section.

These call ``ticket_txn.main(...)`` directly (the lock-holding, committing
entrypoint) so the optimistic-concurrency contract is exercised deterministically
and in-process — not via nondeterministic process-race timing:

  * happy path: a valid transition writes exactly one STATUS event, commits, and
    exits 0;
  * stale-status path: a current_status that no longer matches actual status is
    rejected with EXIT 10 (ConcurrencyError) and writes NO STATUS event.

This is the gate that proves the exit-10 branch survived the heredoc->module
extraction (the structural/race tests can't drive it deterministically).
"""

from __future__ import annotations

import glob
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rebar import _engine

ENGINE_DIR = str(_engine.engine_dir())
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
ticket_txn = importlib.import_module("ticket_txn")
REDUCER = os.path.join(ENGINE_DIR, "ticket-reducer.py")


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return _engine.run(list(args), repo_root=str(repo), cwd=str(repo))


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A repo with one open ticket. Returns (tracker_dir, ticket_id, env_id)."""
    monkeypatch.setenv("_TICKET_TEST_NO_SYNC", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("PROJECT_ROOT", str(repo))
    _run(repo, "init")
    ticket_id = _run(repo, "create", "task", "txn unit ticket").stdout.strip().splitlines()[-1]
    tracker = Path(os.path.realpath(repo / ".tickets-tracker"))
    env_id = (tracker / ".env-id").read_text().strip()
    return tracker, ticket_id, env_id


def _status_events(tracker: Path, ticket_id: str) -> list[str]:
    return [
        p
        for p in glob.glob(str(tracker / ticket_id / "*-STATUS.json"))
        if not os.path.basename(p).startswith(".")
    ]


def _call(tracker: Path, ticket_id: str, env_id: str, current: str, target: str) -> int:
    """Invoke ticket_txn.main as the dispatcher does; return the exit code."""
    argv = [
        "ticket_txn.py",
        "transition",  # operation verb (dispatch)
        str(tracker / ".ticket-write.lock"),
        str(tracker),
        ticket_id,
        current,
        target,
        env_id,
        "unit-test",
        REDUCER,
        "",  # close_reason
        "",  # verdict_hash
        "",  # force_close_reason
    ]
    with pytest.raises(SystemExit) as exc:
        ticket_txn.main(argv)
    code = exc.value.code
    return 0 if code is None else int(code)


def test_transition_happy_path_writes_one_status_and_commits(seeded):
    tracker, ticket_id, env_id = seeded
    before = _status_events(tracker, ticket_id)

    rc = _call(tracker, ticket_id, env_id, "open", "in_progress")
    assert rc == 0

    after = _status_events(tracker, ticket_id)
    assert len(after) == len(before) + 1, "exactly one STATUS event must be appended"

    # The commit happened inside the locked process: no uncommitted TRACKED
    # changes remain (the STATUS event is committed, not left staged). Untracked
    # local artifacts (.ticket-write.lock, the rebuildable .cache.json) are
    # excluded — they are intentionally uncommitted.
    porcelain = subprocess.run(
        ["git", "-C", str(tracker), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert porcelain == "", f"transition must commit inside the lock; tracked changes uncommitted: {porcelain!r}"

    # The new STATUS event is committed (present in HEAD's tree).
    head_tree = subprocess.run(
        ["git", "-C", str(tracker), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert any(line.endswith("-STATUS.json") and line.startswith(ticket_id) for line in head_tree.splitlines()), \
        "the STATUS event must be committed to HEAD"

    status = subprocess.run(
        [sys.executable, REDUCER, str(tracker / ticket_id)],
        capture_output=True, text=True, check=True,
    ).stdout
    import json
    assert json.loads(status)["status"] == "in_progress"


def _status_and_edit_events(tracker: Path, ticket_id: str):
    status = _status_events(tracker, ticket_id)
    edits = [
        p
        for p in glob.glob(str(tracker / ticket_id / "*-EDIT.json"))
        if not os.path.basename(p).startswith(".")
    ]
    return status, edits


def _claim(tracker: Path, ticket_id: str, env_id: str, assignee: str = "") -> int:
    argv = [
        "ticket_txn.py", "claim",
        str(tracker / ".ticket-write.lock"),
        str(tracker),
        ticket_id,
        env_id,
        "unit-test",
        REDUCER,
        assignee,
    ]
    with pytest.raises(SystemExit) as exc:
        ticket_txn.main(argv)
    code = exc.value.code
    return 0 if code is None else int(code)


def test_claim_open_writes_status_and_edit_in_one_commit(seeded):
    tracker, ticket_id, env_id = seeded
    s0, e0 = _status_and_edit_events(tracker, ticket_id)

    rc = _claim(tracker, ticket_id, env_id, assignee="alice")
    assert rc == 0

    s1, e1 = _status_and_edit_events(tracker, ticket_id)
    assert len(s1) == len(s0) + 1, "claim must append exactly one STATUS event"
    assert len(e1) == len(e0) + 1, "claim with assignee must append exactly one EDIT event"

    # Atomic: both committed (no uncommitted tracked changes), in HEAD.
    porcelain = subprocess.run(
        ["git", "-C", str(tracker), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert porcelain == "", f"claim must commit both events; tracked changes uncommitted: {porcelain!r}"

    import json
    state = json.loads(subprocess.run(
        [sys.executable, REDUCER, str(tracker / ticket_id)],
        capture_output=True, text=True, check=True,
    ).stdout)
    assert state["status"] == "in_progress"
    assert state.get("assignee") == "alice", "claim must set the assignee"


def test_claim_non_open_rejected_exit_10_no_event(seeded):
    tracker, ticket_id, env_id = seeded
    # First claim wins → in_progress.
    assert _claim(tracker, ticket_id, env_id, assignee="alice") == 0
    s0, e0 = _status_and_edit_events(tracker, ticket_id)

    # Second claim must be rejected (not open) with exit 10 and write nothing.
    rc = _claim(tracker, ticket_id, env_id, assignee="bob")
    assert rc == 10, "claiming a non-open ticket must be rejected with exit 10"
    s1, e1 = _status_and_edit_events(tracker, ticket_id)
    assert (s1, e1) == (s0, e0), "a rejected claim must not write any event"


def test_transition_stale_status_rejected_exit_10_no_event(seeded):
    tracker, ticket_id, env_id = seeded
    before = _status_events(tracker, ticket_id)

    # Actual status is 'open'; claim it is 'blocked' → optimistic-concurrency miss.
    rc = _call(tracker, ticket_id, env_id, "blocked", "closed")
    assert rc == 10, "stale current_status must be rejected with exit 10 (ConcurrencyError)"

    after = _status_events(tracker, ticket_id)
    assert after == before, "a rejected transition must NOT write a STATUS event"


# ── Verdict-hash gate: fail-CLOSED on an unreadable config ────────────────────
# Regression for the fail-open hole: a *present* verify config that cannot be
# read/parsed must require the verdict (block the close), never silently disable
# the gate. An *absent* config is the intended opt-out (gate stays off).


@pytest.fixture
def seeded_story(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A repo with one in_progress STORY. Returns (tracker, story_id, env_id, root)."""
    monkeypatch.setenv("_TICKET_TEST_NO_SYNC", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("PROJECT_ROOT", str(repo))
    _run(repo, "init")
    story_id = _run(repo, "create", "story", "verdict gate story").stdout.strip().splitlines()[-1]
    tracker = Path(os.path.realpath(repo / ".tickets-tracker"))
    env_id = (tracker / ".env-id").read_text().strip()
    # Move open -> in_progress so the next transition under test is the close.
    assert _call(tracker, story_id, env_id, "open", "in_progress") == 0
    return tracker, story_id, env_id, repo


def test_close_story_with_unreadable_verify_config_fails_closed(seeded_story):
    tracker, story_id, env_id, root = seeded_story
    cfg_dir = root / ".rebar"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # The gate-enabling line is present, but a stray invalid UTF-8 byte makes the
    # line-iteration decode raise — the path the fix must treat as fail-closed.
    (cfg_dir / "config.conf").write_bytes(b"verify.require_verdict_for_close=true\n\xff bad\n")

    before = _status_events(tracker, story_id)
    # Close with an empty verdict-hash: gate ON (fail-closed) must reject.
    rc = _call(tracker, story_id, env_id, "in_progress", "closed")
    assert rc == 1, "an unreadable verify config must fail CLOSED (block the story close)"
    after = _status_events(tracker, story_id)
    assert after == before, "a blocked close must not append a STATUS event"


def test_close_story_with_no_verify_config_is_opt_out(seeded_story):
    tracker, story_id, env_id, root = seeded_story
    # Control: no config at all → the gate is opt-in/off → the story closes.
    assert not (root / ".rebar" / "config.conf").exists()
    rc = _call(tracker, story_id, env_id, "in_progress", "closed")
    assert rc == 0, "with no verify config present the gate stays off; the close succeeds"
