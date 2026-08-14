"""Active-only scan scoping + archive-time terminal fold (story detoxicant-pointless-paperwasp).

THE PROBLEM. The store-walking maintenance surfaces — ``rebar fsck``'s per-ticket checks and
``rebar compact-all``'s selection sweep — visited EVERY ticket directory, archived or not. An
archive is a statement that a ticket is settled; re-reading its whole event log on every sweep
makes maintenance cost grow with store HISTORY instead of store ACTIVITY.

THE FIX (asserted here), in two halves:

* **Terminal fold at archive time**: ``rebar archive`` folds the ticket's entire live log into
  a SNAPSHOT right before writing the ARCHIVED event, reusing the single-ticket fold path with
  the incremental gates bypassed (threshold 0, horizon now). An archived ticket therefore
  carries no unfolded tail — which is what makes skipping it SAFE. A ticket with nothing
  unfolded is not re-folded (no empty/duplicate SNAPSHOT), and a failed fold aborts the
  archive (the archived⇒folded invariant is never published falsely).

* **Active-only default scoping**: the shared ticket-dir iterator yields only ACTIVE tickets
  by default — a dir is skipped only when its ``.archived`` marker exists AND the event log
  net-confirms archival (marker alone never decides; a stale marker on a reverted archive is
  still visited). ``--include-archived`` restores the full walk on both ``fsck`` and
  ``compact-all``.
"""

from __future__ import annotations

import json
import subprocess
import uuid as _uuid
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._commands import fsck as _fsck
from rebar._commands import fsck_repair as _fsck_repair
from rebar._store import hlc

pytestmark = pytest.mark.unit

# Two hours in ns — comfortably older than the 30-minute default compaction horizon.
_TWO_HOURS_NS = 2 * 3_600_000_000_000


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _tdir(repo: Path, tid: str) -> Path:
    return _tracker(repo) / tid


def _seed(repo: Path, title: str, comments: int = 2) -> str:
    tid = rebar.create_ticket("task", title, description="x" * 60, repo_root=str(repo))
    for i in range(comments):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    return tid


def _age_events(tdir: Path, by_ns: int) -> None:
    """Rewrite every live event's timestamp to be `by_ns` older (the filename carries the
    timestamp prefix, so rename too) — makes events fall outside the compaction horizon."""
    for path in sorted(tdir.glob("*.json")):
        if path.name.startswith(".") or path.name.endswith("-SNAPSHOT.json"):
            continue
        event = json.loads(path.read_text())
        ts = event.get("timestamp")
        if not isinstance(ts, int):
            continue
        event["timestamp"] = ts - by_ns
        rest = path.name.split("-", 1)[1]
        path.write_text(json.dumps(event))
        path.rename(path.parent / f"{event['timestamp']}-{rest}")


def _write_event(tdir: Path, event_type: str, data: dict | None = None, *, age_ns: int = 0) -> str:
    """Drop a raw event file into a ticket dir, bypassing append_event (simulates events
    written by an older rebar — e.g. a ticket archived BEFORE archive folded inline)."""
    ts = hlc.physical_now() - age_ns
    uid = str(_uuid.uuid4())
    event = {
        "event_type": event_type,
        "uuid": uid,
        "timestamp": ts,
        "data": data or {},
    }
    (tdir / f"{ts}-{event_type}.json").write_text(json.dumps(event))
    return uid


def _snapshots(tdir: Path) -> list[str]:
    return sorted(p.name for p in tdir.glob("*-SNAPSHOT.json"))


def _archive_legacy(repo: Path, tid: str) -> None:
    """Archive the way pre-fold rebar did: ARCHIVED event + marker, NO terminal fold."""
    tdir = _tdir(repo, tid)
    _write_event(tdir, "ARCHIVED")
    (tdir / ".archived").write_text("")


# ── the terminal fold: archive leaves no unfolded tail ───────────────────────────────────────
def test_archive_folds_the_live_log_inline(store: Path) -> None:
    """RED pre-fix: archive wrote only the ARCHIVED event, leaving the whole log unfolded."""
    repo = store
    tid = _seed(repo, "archive folds me", comments=3)
    tdir = _tdir(repo, tid)
    assert not _snapshots(tdir), "precondition: never folded"

    rebar.archive(tid, repo_root=str(repo))

    assert _snapshots(tdir), "archive must fold the live log into a SNAPSHOT inline"
    assert (tdir / ".archived").exists()
    # Everything that existed before the archive is folded: the ONLY live (non-SNAPSHOT)
    # event left is the ARCHIVED event itself, which is written after the fold.
    live = [
        p.name
        for p in tdir.glob("*.json")
        if not p.name.startswith(".") and not p.name.endswith("-SNAPSHOT.json")
    ]
    assert len(live) == 1 and live[0].endswith("-ARCHIVED.json"), live


def test_archive_folds_events_inside_the_incremental_horizon(store: Path) -> None:
    """The terminal fold bypasses the incremental gates: freshly-written events (inside the
    30-minute horizon, under the threshold) are still folded. RED pre-fix; also kills the
    mutant that reuses the configured threshold/horizon instead of the terminal 0/now."""
    repo = store
    tid = _seed(repo, "young events fold too", comments=1)
    tdir = _tdir(repo, tid)

    rebar.archive(tid, repo_root=str(repo))

    assert _snapshots(tdir), "young/under-threshold events must not block the terminal fold"
    live = [
        p.name
        for p in tdir.glob("*.json")
        if not p.name.startswith(".") and not p.name.endswith("-SNAPSHOT.json")
    ]
    assert len(live) == 1 and live[0].endswith("-ARCHIVED.json"), live


def test_archive_of_a_settled_ticket_writes_no_new_snapshot(store: Path) -> None:
    """No-op guard: a ticket whose log is already fully folded is not re-folded — no empty or
    duplicate SNAPSHOT (kills the mutant that drops the nothing-unfolded guard)."""
    repo = store
    tid = _seed(repo, "settled before archive", comments=1)
    tdir = _tdir(repo, tid)
    assert (
        _compact.compact_cli(
            [tid, "--threshold=0", "--horizon=0", "--skip-sync"], repo_root=str(repo)
        )
        == 0
    )
    before = _snapshots(tdir)
    assert before, "precondition: fully folded"

    rebar.archive(tid, repo_root=str(repo))

    assert _snapshots(tdir) == before, "archive must not write a SNAPSHOT when nothing unfolded"
    assert any(p.name.endswith("-ARCHIVED.json") for p in tdir.iterdir())


# ── the shared iterator: marker alone never decides ──────────────────────────────────────────
def test_ticket_dirs_defaults_to_active_only(store: Path) -> None:
    repo = store
    active = _seed(repo, "stays active")
    archived = _seed(repo, "gets archived")
    rebar.archive(archived, repo_root=str(repo))
    tracker = str(_tracker(repo))

    assert active in _fsck_repair._ticket_dirs(tracker)
    assert archived not in _fsck_repair._ticket_dirs(tracker)
    assert archived in _fsck_repair._ticket_dirs(tracker, include_archived=True)


def test_stale_marker_on_a_reverted_archive_does_not_skip(store: Path) -> None:
    """A `.archived` marker whose event log does NOT net-confirm archival (revert landed, or
    the marker is simply stale) must not hide the ticket — marker alone never decides."""
    repo = store
    tid = _seed(repo, "marker lies")
    tdir = _tdir(repo, tid)
    (tdir / ".archived").write_text("")  # stale marker, no ARCHIVED event at all
    tracker = str(_tracker(repo))
    assert tid in _fsck_repair._ticket_dirs(tracker)

    # And the reverted-archive shape: ARCHIVED + REVERT targeting it → net NOT archived.
    tid2 = _seed(repo, "archive then revert")
    tdir2 = _tdir(repo, tid2)
    archived_uuid = _write_event(tdir2, "ARCHIVED")
    _write_event(tdir2, "REVERT", {"target_event_uuid": archived_uuid})
    (tdir2 / ".archived").write_text("")
    assert tid2 in _fsck_repair._ticket_dirs(tracker)


# ── fsck: default active-only, --include-archived restores the full walk ─────────────────────
def test_fsck_default_skips_archived_and_flag_restores(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An archived ticket with a corrupt event: invisible to the default scan, reported under
    --include-archived (text and JSON)."""
    repo = store
    _seed(repo, "healthy active")
    archived = _seed(repo, "archived with corrupt event")
    rebar.archive(archived, repo_root=str(repo))
    corrupt = _tdir(repo, archived) / "9999999999999999999-COMMENT.json"
    corrupt.write_text("{not json")

    rc_default = _fsck.fsck_cli([], repo_root=str(repo), no_mutate=True)
    out_default = capsys.readouterr().out
    assert corrupt.name not in out_default, "default fsck must not visit archived tickets"
    assert rc_default == 0

    rc_flag = _fsck.fsck_cli(["--include-archived"], repo_root=str(repo), no_mutate=True)
    out_flag = capsys.readouterr().out
    assert corrupt.name in out_flag, "--include-archived must restore the full walk"
    assert rc_flag == 1

    rc_json = _fsck.fsck_cli(
        ["--output", "json", "--include-archived"], repo_root=str(repo), no_mutate=True
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc_json == 1
    assert corrupt.name in json.dumps(payload), "JSON output must carry the archived finding"


# ── compact-all: default active-only, --include-archived sweeps legacy archives ──────────────
def test_compact_all_dry_run_skips_archived_by_default(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A legacy-archived ticket (archived before the fold existed, unfolded tail) is not
    selected by default; --include-archived selects it."""
    repo = store
    active = _seed(repo, "active and foldable", comments=2)
    legacy = _seed(repo, "legacy archived, unfolded", comments=2)
    _archive_legacy(repo, legacy)
    _age_events(_tdir(repo, active), _TWO_HOURS_NS)
    _age_events(_tdir(repo, legacy), _TWO_HOURS_NS)

    assert _compact.compact_all_cli(["--dry-run"], repo_root=str(repo)) == 0
    out = capsys.readouterr().out
    assert active in out
    assert legacy not in out, "default sweep must not select archived tickets"

    assert _compact.compact_all_cli(["--dry-run", "--include-archived"], repo_root=str(repo)) == 0
    out = capsys.readouterr().out
    assert active in out
    assert legacy in out, "--include-archived must sweep legacy-archived tickets"


def test_compact_all_include_archived_folds_a_legacy_archive(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The migration door: a pre-change-archived unfolded ticket is folded by
    `compact-all --include-archived`, after which it carries no unfolded tail."""
    repo = store
    legacy = _seed(repo, "legacy archive to fold", comments=2)
    _archive_legacy(repo, legacy)
    tdir = _tdir(repo, legacy)
    _age_events(tdir, _TWO_HOURS_NS)
    assert not _snapshots(tdir)

    assert _compact.compact_all_cli(["--include-archived"], repo_root=str(repo)) == 0
    capsys.readouterr()

    assert _snapshots(tdir), "the flagged sweep must fold the legacy archive"


def test_empty_store_scans_cleanly(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = store
    assert _fsck.fsck_cli([], repo_root=str(repo), no_mutate=True) == 0
    capsys.readouterr()
    assert _compact.compact_all_cli(["--dry-run"], repo_root=str(repo)) == 0
    out = capsys.readouterr().out
    assert "Tickets needing compaction    : 0" in out


# ── a failed terminal fold aborts the archive ─────────────────────────────────────────────────
def test_a_failed_terminal_fold_aborts_the_archive(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery contract: fold rc != 0 → CommandError carrying the fold's output, and NO
    ARCHIVED event or marker is written (the archived⇒folded invariant is never published
    falsely)."""
    from rebar._commands import leaf as _leaf
    from rebar._commands._seam import CommandError

    repo = store
    tid = _seed(repo, "fold blows up", comments=2)
    tdir = _tdir(repo, tid)

    def _failing_fold(argv: list[str], **_kw) -> int:
        print("boom: simulated fold failure detail")
        return 1

    monkeypatch.setattr(_compact, "compact_cli", _failing_fold)
    with pytest.raises(CommandError) as excinfo:
        _leaf.archive(tid, repo_root=str(repo))

    assert "terminal fold failed" in str(excinfo.value)
    assert "boom: simulated fold failure detail" in str(excinfo.value)
    assert not any(p.name.endswith("-ARCHIVED.json") for p in tdir.iterdir())
    assert not (tdir / ".archived").exists()


# ── the repair path honors the flag too ───────────────────────────────────────────────────────
def test_repair_run_scopes_archived_findings_with_the_flag(store: Path) -> None:
    """`fsck --repair` must not REPORT archived findings it will never REPAIR: the repair
    sweep walks the same scoped iterator, and --include-archived restores both."""
    from rebar._commands.fsck_repair import _repair_run

    repo = store
    tid = _seed(repo, "archived with repairable fault", comments=2)
    tdir = _tdir(repo, tid)
    assert (
        _compact.compact_cli(
            [tid, "--threshold=0", "--horizon=0", "--skip-sync"], repo_root=str(repo)
        )
        == 0
    )
    retired = sorted(tdir.glob("*.json.retired"))
    assert retired, "precondition: the fold retired the folded sources"
    # Re-materialize one folded source: a still-present source is the SNAPSHOT_INCONSISTENT
    # fault whose repair is a retire.
    source = retired[0]
    (tdir / source.name[: -len(".retired")]).write_text(source.read_text())
    _archive_legacy(repo, tid)
    tracker = str(_tracker(repo))

    lines, _n = _repair_run(tracker, dry_run=True)
    assert tid not in "\n".join(lines), "default repair must not plan work on archived tickets"

    lines, _n = _repair_run(tracker, dry_run=True, include_archived=True)
    assert tid in "\n".join(lines), "--include-archived must extend the repair sweep too"


# ── store-wide metrics stay store-wide ────────────────────────────────────────────────────────
def test_authorship_tallies_still_count_archived_tickets(store: Path) -> None:
    """The authorship presence tallies are store-wide METRICS, not per-ticket findings —
    default scoping must not silently narrow them."""
    from rebar._commands.fsck_authorship import EnvAuthorshipTally
    from rebar._commands.fsck_scan import _check_create_events, _check_json_validity

    repo = store
    active = _seed(repo, "active for tally", comments=1)
    archived = _seed(repo, "archived for tally", comments=1)
    rebar.archive(archived, repo_root=str(repo))
    tracker = str(_tracker(repo))

    _lines, _issues, signed_default, unsigned_default = _check_create_events(tracker)
    _lines, _issues, signed_full, unsigned_full = _check_create_events(
        tracker, include_archived=True
    )
    assert (signed_default + unsigned_default) == (signed_full + unsigned_full) > 0, (
        "the store-wide authorship presence tally must not shrink under default scoping"
    )

    tally_default = EnvAuthorshipTally()
    _check_json_validity(tracker, tally_default)
    tally_full = EnvAuthorshipTally()
    _check_json_validity(tracker, tally_full, include_archived=True)

    def _shape(tally: EnvAuthorshipTally) -> object:
        return {
            env: {slot: getattr(row, slot) for slot in row.__slots__}
            for env, row in tally._envs.items()
        }

    assert _shape(tally_default) == _shape(tally_full), (
        "the per-env authorship tally must observe archived tickets' events either way"
    )
    del active


# ── the flag is documented where usage lives ─────────────────────────────────────────────────
def test_usage_lines_name_the_flag(capsys: pytest.CaptureFixture[str]) -> None:
    help_dir = Path(_fsck.__file__).resolve().parents[1] / "_cli" / "help"
    assert "--include-archived" in (help_dir / "fsck.txt").read_text()
    assert "--include-archived" in (help_dir / "compact-all.txt").read_text()

    assert _compact.compact_all_cli(["--help"]) == 0
    assert "--include-archived" in capsys.readouterr().out
