"""Keep lock-held commits separate from foreground maintenance (bd66-28a4-fd31-4c9f).

Git 2.47+ may run ``maintenance --auto`` inside ``git commit``. With ADR 0051's
``maintenance.autoDetach=false``, crossing the default loose-object threshold could put an
O(store) repack inside the ``_GIT_TIMEOUT`` subprocess, kill it mid-repack, and lose the write.
The fix retains the 30-second commit bound, suppresses that implicit maintenance, then runs an
explicit watchdog-budgeted step under the same lock. These tests cross the threshold and read
git's Trace2 process tree: no maintenance, gc or repack process may be a DESCENDANT of a
``git commit``. The invariant is structural. A wall-clock kill bound (the first version used
1 s) also fails on any commit starved by host load, with no maintenance involved (bug 99fb).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from _git_upkeep import _trace_events

import rebar
from rebar._commands._seam import tracker_dir
from rebar._store import event_commit_git, gitutil

pytestmark = pytest.mark.unit


def _git(d: str, *a: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=False)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=r, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    rebar.init_repo(repo_root=str(r))
    return r


# Commands that make up the O(store) work the suppression keeps out of a bounded commit.
_MAINTENANCE_COMMANDS = frozenset(
    {"maintenance", "gc", "repack", "pack-objects", "multi-pack-index"}
)


def _git_subcommand(argv: list[str]) -> str | None:
    """The git subcommand in a traced argv, skipping the executable and ``-c``/``-C`` options."""
    i = 1
    while i < len(argv):
        if argv[i] in ("-c", "-C"):
            i += 2
        elif argv[i].startswith("-"):
            i += 1
        else:
            return argv[i]
    return None


def _maintenance_inside_commits(trace: Path) -> tuple[int, list[str]]:
    """``(commits traced, maintenance processes that are descendants of a commit)``.

    A process's Trace2 ``sid`` is its parent's ``sid`` plus ``/`` plus its own, so a commit's
    descendants are the processes whose ``sid`` starts with the commit's ``sid`` and a slash.
    The explicit post-commit maintenance step is a separate top-level process, so its repack
    children are not counted: only work done INSIDE a commit is.
    """
    starts = [
        (str(event.get("sid", "")), [str(a) for a in event.get("argv") or []])
        for event in _trace_events(trace)
        if event.get("event") == "start"
    ]
    commit_sids = {
        sid for sid, argv in starts if "/" not in sid and _git_subcommand(argv) == "commit"
    }
    offenders = [
        " ".join(argv[:4])
        for sid, argv in starts
        if any(sid.startswith(f"{c}/") for c in commit_sids)
        and _git_subcommand(argv) in _MAINTENANCE_COMMANDS
    ]
    return len(commit_sids), offenders


def _maintenance_diagnosis(trace: Path, tracker: str) -> str:
    """Name which of the detector's failure mechanisms the trace shows (bug ab03-0276).

    An empty ``offenders`` list reads the same whether git declined to start auto-maintenance,
    started it outside every commit, or cut it short inside one. The trace file lives under
    ``tmp_path`` and is rotated away by later pytest sessions, so the evidence that tells these
    apart goes into the failure message, where the run's log keeps it.
    """
    events = list(_trace_events(trace))
    starts = {
        str(e.get("sid", "")): [str(a) for a in e.get("argv") or []]
        for e in events
        if e.get("event") == "start"
    }
    exits = {str(e.get("sid", "")): e.get("t_abs") for e in events if e.get("event") == "exit"}
    maintenance = sorted(
        (sid, " ".join(argv[:4]))
        for sid, argv in starts.items()
        if _git_subcommand(argv) in _MAINTENANCE_COMMANDS
    )
    commits = [
        f"{sid} {exits.get(sid)}s"
        for sid, argv in starts.items()
        if "/" not in sid and _git_subcommand(argv) == "commit"
    ]
    # A top-level maintenance process is rebar's explicit post-commit step, which runs whether
    # or not git chose auto-maintenance; only a nested one is git's own auto-maintenance.
    nested = [m for m in maintenance if "/" in m[0]]
    if not nested:
        verdict = (
            "git started no auto-maintenance under any process: it chose not to run it "
            f"(top-level explicit steps: {len(maintenance)})"
        )
    else:
        verdict = "git ran auto-maintenance, but not as a descendant of any traced commit"
    gc_log = _git(tracker, "rev-parse", "--git-path", "gc.log").stdout.strip()
    gc_path = Path(gc_log) if os.path.isabs(gc_log) else Path(tracker) / gc_log
    gc_text = (
        gc_path.read_text(errors="replace", encoding="utf-8")[:500]
        if gc_path.is_file()
        else "<absent>"
    )
    return (
        f"{verdict}.\n  maintenance processes: {maintenance or 'none'}"
        f"\n  commits (sid, exit t_abs): {commits or 'none'}"
        f"\n  gc.log: {gc_text}"
        f"\n  count-objects -v: {_git(tracker, 'count-objects', '-v').stdout.split()}"
    )


def _sampled_loose_count(tracker: str) -> int:
    """Loose objects in ``objects/17``: the one directory git's auto-gc samples. Git starts
    auto-gc when this count exceeds ``ceil(gc.auto / 256)`` (``too_many_loose_objects``)."""
    sampled = Path(_git(tracker, "rev-parse", "--git-path", "objects/17").stdout.strip())
    sampled = sampled if sampled.is_absolute() else Path(tracker) / sampled
    return len(list(sampled.iterdir())) if sampled.is_dir() else 0


def _assert_foreground_maintenance(tracker: str) -> None:
    # Precondition: rebar's gc-config makes auto-maintenance FOREGROUND (so it lands inside the
    # commit subprocess). If this ever changes, the mechanism under test no longer applies.
    assert _git(tracker, "config", "--get", "maintenance.autoDetach").stdout.strip() == "false"


# Which threshold auto-maintenance applies depends on git's default strategy: the gc strategy
# reads gc.auto, the geometric one (git 2.56 here) reads maintenance.geometric-repack.auto.
# Command-scope settings outrank the store's config, which rebar's ensure sweep rewrites.
_MINIMAL_THRESHOLDS = (("gc.auto", "1"), ("maintenance.geometric-repack.auto", "1"))


def _cross_a_minimal_maintenance_threshold(repo: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Make git's next auto-maintenance check say "run" while leaving it almost nothing to
    pack (bug ab03-0276).

    The detector only has to show that git starts maintenance inside a commit once the
    suppression is gone. With the 20,000-object fill, the commit it observes also has to
    finish that repack inside the store's per-call git bound; under host load it did not
    ("git timed out after 30s"), so the detector failed before checking anything. Here
    gc.auto=1 gives a sampled threshold of ceil(1/256) = 1, which two loose objects whose ids
    start with ``17`` exceed, and geometric-repack.auto=1 is met by those same two objects.
    The repack then covers only the few objects a fresh store holds.
    """
    tracker = str(tracker_dir(str(repo)))
    _assert_foreground_maintenance(tracker)
    contents: list[bytes] = []
    i = 0
    while len(contents) < 2:
        body = f"bd66-sampled-{i}\n".encode()
        if hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest().startswith("17"):
            contents.append(body)
        i += 1
    for body in contents:
        oid = (
            subprocess.run(
                ["git", "-C", tracker, "hash-object", "-w", "--stdin"],
                input=body,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        assert oid.startswith("17"), f"store is not SHA-1, so {oid} misses objects/17"
    assert _sampled_loose_count(tracker) > 1
    for index, (key, value) in enumerate(_MINIMAL_THRESHOLDS):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(_MINIMAL_THRESHOLDS)))
    return tracker


def _create_two_tickets_traced(
    repo: Path, monkeypatch: pytest.MonkeyPatch, trace: Path
) -> tuple[int, list[str]]:
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    # The report's own reproduction: a fixture creating a ticket in the store. Must not raise.
    tid = rebar.create_ticket("task", "bd66 create after loose-fill", repo_root=str(repo))
    assert tid, "create_ticket returned no id"
    # The write is durable and the store is not wedged: a second create also succeeds.
    tid2 = rebar.create_ticket("task", "bd66 second create", repo_root=str(repo))
    assert tid2 and tid2 != tid
    return _maintenance_inside_commits(trace)


def test_write_survives_foreground_maintenance_repack(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The minimal crossing makes git's next auto-maintenance check say "run" without the
    # 20,000-object loose fill, which cost ~22 ms per object on Windows and pushed this test
    # past the 600 s budget (bug 80f4-1d94). The detector test below proves the same setup
    # starts maintenance inside a commit once the suppression is removed.
    _cross_a_minimal_maintenance_threshold(repo, monkeypatch)

    commits, offenders = _create_two_tickets_traced(repo, monkeypatch, tmp_path / "trace.json")

    assert commits >= 2, f"the trace must contain the ticket-create commits (saw {commits})"
    assert not offenders, (
        "git ran maintenance INSIDE a bounded commit, so a repack can be killed mid-way and "
        f"lose the write: {offenders}"
    )


def test_the_trace_check_detects_maintenance_inside_a_commit(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The check must be able to fail: with the suppression removed, git runs
    ``maintenance run --auto`` (and the repack below it) as a child of the commit."""
    tracker = _cross_a_minimal_maintenance_threshold(repo, monkeypatch)
    monkeypatch.setattr(event_commit_git, "_AUTOMAINT_OFF", ())
    # Bounded git children also carry the suppression in their environment (bug 51e0-5038).
    monkeypatch.setattr(gitutil, "_IMPLICIT_MAINTENANCE_OFF", ())

    trace = tmp_path / "trace.json"
    commits, offenders = _create_two_tickets_traced(repo, monkeypatch, trace)

    assert commits >= 1
    assert any("maintenance" in o for o in offenders), (
        "removing the suppression produced no maintenance descendant of a commit: "
        f"{offenders}\n{_maintenance_diagnosis(trace, tracker)}"
    )


def _write_trace(trace: Path, *processes: tuple[str, list[str], float]) -> None:
    lines = []
    for sid, argv, t_abs in processes:
        lines.append(json.dumps({"event": "start", "sid": sid, "argv": argv}))
        lines.append(json.dumps({"event": "exit", "sid": sid, "t_abs": t_abs}))
    trace.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_diagnosis_says_git_chose_not_to_run_maintenance(repo: Path, tmp_path: Path) -> None:
    """With no maintenance-family process in the trace, the message names git's choice."""
    trace = tmp_path / "trace.json"
    # rebar's own top-level maintenance step does not count as git choosing to run it.
    _write_trace(
        trace,
        ("a1", ["git", "commit", "-q"], 0.4),
        ("d4", ["git", "maintenance", "run"], 1.0),
    )
    message = _maintenance_diagnosis(trace, str(tracker_dir(str(repo))))
    assert "chose not to run it (top-level explicit steps: 1)" in message
    assert "a1 0.4s" in message
    assert "gc.log: <absent>" in message


def test_diagnosis_says_maintenance_ran_outside_the_commit(repo: Path, tmp_path: Path) -> None:
    """Auto-maintenance nested under a process other than the commit is named as such."""
    trace = tmp_path / "trace.json"
    _write_trace(
        trace,
        ("a1", ["git", "commit", "-q"], 0.4),
        ("b2", ["git", "add", "-A"], 0.2),
        ("b2/c3", ["git", "maintenance", "run", "--auto"], 3.1),
    )
    message = _maintenance_diagnosis(trace, str(tracker_dir(str(repo))))
    assert "not as a descendant of any traced commit" in message
    assert "git maintenance run --auto" in message
