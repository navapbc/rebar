"""Register the ``multi-project-bridge`` store-compat capability (story 67f9).

The one behaviour under test is that ``multi-project-bridge`` is a *registered* member
of :data:`rebar._store.compat.KNOWN_CAPABILITIES`, so a committed
``.store-compat.json`` record that declares it classifies as **compatible** on this
binary instead of failing the fail-closed gate. Everything else here is a regression
guard proving the registration widened the known set by exactly one entry and did not
disable, narrow, or bypass the gate:

- Contrast: an *unregistered* capability name still raises ``StoreIncompatibleError``.
- Absent record: a store with no ``.store-compat.json`` still passes through as
  implicit legacy.
- Write-gate scoping: a real store declaring an unknown capability refuses a
  lock-held write while ``list``/``show`` against the same store still succeed.
- Corrupt record: a malformed record still raises rather than being read as absent.

Tests assert OBSERVABLE behaviour only — the classification function's return/raise,
CLI exit codes, and on-disk event files — never internal structure.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar
from rebar._store import compat

COMPAT_FILE = ".store-compat.json"
MULTI_PROJECT_BRIDGE = "multi-project-bridge"


# ── helpers ───────────────────────────────────────────────────────────────────
def _write_record(tracker: Path, obj: object) -> None:
    """Write a ``.store-compat.json`` record (a dict is JSON-encoded; a str is
    written verbatim so corrupt/truncated payloads can be exercised)."""
    body = obj if isinstance(obj, str) else json.dumps(obj)
    (tracker / COMPAT_FILE).write_text(body, encoding="utf-8")


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    """A bare tracker directory the classification function reads directly.

    :func:`compat.check_store_compat` opens ``<tracker>/.store-compat.json`` from the
    worktree, so a plain directory (no git/lock machinery) is enough to exercise the
    read-parse-validate core in isolation.
    """
    d = tmp_path / "tracker"
    d.mkdir()
    return d


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real initialized rebar store, for the write-gate (lock-acquisition) case."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker_dir(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=subprocess_env(),
    )


def _seed(repo: Path) -> str:
    return rebar.create_ticket(
        "task",
        "Compat multi-project task",
        description="Body.\n\n## Acceptance Criteria\n- [ ] a",
        repo_root=str(repo),
    )


def _event_files(repo: Path, tid: str) -> set[str]:
    tdir = _tracker_dir(repo) / tid
    return {p.name for p in tdir.glob("*.json") if not p.name.startswith(".")}


# ── happy path: the registration itself (RED before the one-line change) ──────
def test_multi_project_bridge_is_registered() -> None:
    assert MULTI_PROJECT_BRIDGE in compat.KNOWN_CAPABILITIES


def test_record_requiring_multi_project_bridge_classifies_compatible(tracker: Path) -> None:
    _write_record(
        tracker,
        {"format_version": 1, "required_capabilities": [MULTI_PROJECT_BRIDGE]},
    )
    # Compatible -> the fail-closed gate does not fire, and the non-raising twin
    # reports no problem.
    compat.check_store_compat(str(tracker))
    assert compat.describe_store_compat(str(tracker)) is None


# ── contrast: an UNREGISTERED capability still fails closed ───────────────────
def test_unregistered_capability_still_raises(tracker: Path) -> None:
    _write_record(
        tracker,
        {"format_version": 1, "required_capabilities": ["totally-unknown-cap-v99"]},
    )
    with pytest.raises(compat.StoreIncompatibleError) as ei:
        compat.check_store_compat(str(tracker))
    assert "totally-unknown-cap-v99" in str(ei.value)
    problem = compat.describe_store_compat(str(tracker))
    assert problem is not None and problem["kind"] == "unknown_capability"


# ── absent-record regression: implicit legacy still passes through ────────────
def test_absent_record_passes_through(tracker: Path) -> None:
    assert not (tracker / COMPAT_FILE).exists()
    compat.check_store_compat(str(tracker))  # no raise
    assert compat.describe_store_compat(str(tracker)) is None


# ── write-gate scoping (real store): writes refused, reads allowed ────────────
def test_unknown_capability_blocks_write_but_not_reads(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)  # seed while still compatible
    before = _event_files(rebar_repo, tid)
    _write_record(
        _tracker_dir(rebar_repo),
        {"format_version": 1, "required_capabilities": ["still-unknown-cap"]},
    )
    # A lock-held write is refused with a non-zero exit, and appends no event.
    write = _cli("comment", tid, "should be blocked", cwd=rebar_repo)
    assert write.returncode != 0, f"write was NOT blocked: {write.stdout}{write.stderr}"
    assert "still-unknown-cap" in (write.stdout + write.stderr)
    assert _event_files(rebar_repo, tid) == before, "an event was written despite the gate"
    # Reads against the same incompatible store stay available (they hold no lock).
    for args in (("show", tid), ("list",)):
        read = _cli(*args, cwd=rebar_repo)
        assert read.returncode == 0, f"read command {args} was blocked: {read.stderr}"


# ── corrupt-record path: a malformed record fails closed, not "absent" ────────
def test_corrupt_record_raises_and_names_path(tracker: Path) -> None:
    _write_record(tracker, '{"format_version": 1, "required_capab')  # truncated JSON
    with pytest.raises(compat.StoreIncompatibleError) as ei:
        compat.check_store_compat(str(tracker))
    assert COMPAT_FILE in str(ei.value), "diagnostic must name the record path"
