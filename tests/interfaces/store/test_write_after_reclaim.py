"""A ticket write on a clone that a reclaim publish-swap superseded (bug smiling-ancient-finwhale).

A reclaim replaces the shared ``tickets`` history with a parentless baseline whose store
epoch record names the epoch it replaced. A clone that has fetched that baseline shares no
commit with it, but the lineage proves the new history supersedes the clone's, so the clone
must move onto it (ADR 0106 §5) rather than refuse every write as ``DIVERGED``.

Each test drives the shared write seam through the library (``rebar.comment``), the path the
MCP server uses, against a real local bare origin. The library path does not run the CLI's
pre-write reconverge, so the write precheck is the first code to see the superseded clone.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from _git_upkeep import init_bare_remote

import rebar
from rebar._engine_support import reads
from rebar._store import compat

NEW_EPOCH = "finwhale-reclaimed-epoch"


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout.strip()


def _git_rc(*args: str, cwd: Path) -> int:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False).returncode


@pytest.fixture
def shared_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, str]:
    """An initialized repo whose tracker is converged with a real bare origin.

    Yields ``(repo, tracker, origin, seed_ticket_id)``.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "work"
    init_bare_remote(origin)
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t.co", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    rebar.init_repo(repo_root=str(repo))
    tracker = repo / ".tickets-tracker"
    seed = rebar.create_ticket("task", "seed", repo_root=str(repo))
    _git("fetch", "-q", "origin", "tickets", cwd=tracker)
    assert _git("rev-parse", "HEAD", cwd=tracker) == _git(
        "rev-parse", "origin/tickets", cwd=tracker
    )
    return repo, tracker, origin, seed


def _publish_rewritten_baseline(tmp_path: Path, origin: Path, record: dict) -> str:
    """Force-publish a parentless copy of origin's tree stamped with ``record``."""
    publisher = tmp_path / "publisher"
    _git("clone", "-q", "-b", "tickets", str(origin), str(publisher), cwd=tmp_path)
    _git("config", "user.email", "p@t.co", cwd=publisher)
    _git("config", "user.name", "p", cwd=publisher)
    _git("checkout", "-q", "--orphan", "reclaimed", cwd=publisher)
    (publisher / compat.COMPAT_FILENAME).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _git("add", "-A", cwd=publisher)
    _git("commit", "-q", "--no-verify", "-m", "reclaimed baseline", cwd=publisher)
    _git("push", "-q", "-f", "origin", "HEAD:tickets", cwd=publisher)
    return _git("rev-parse", "HEAD", cwd=publisher)


def _mark_recent_read(tracker: Path) -> None:
    """Record a read-path reconverge as just run, as on a server that reads constantly.

    The read path reconverges at most once a minute; inside that window the write's own
    reads skip it, so nothing moves the clone before the write precheck runs.
    """
    marker = reads.sync_marker_path(str(tracker.resolve()))
    Path(marker).write_text(str(int(time.time())), encoding="utf-8")


def _local_record(tracker: Path) -> dict:
    return json.loads((tracker / compat.COMPAT_FILENAME).read_text(encoding="utf-8"))


def _remote_tip(origin: Path) -> str:
    return _git("rev-parse", "refs/heads/tickets", cwd=origin)


def test_write_on_a_superseded_clone_lands_on_the_reclaimed_remote(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    repo, tracker, origin, seed = shared_store
    baseline = _publish_rewritten_baseline(
        tmp_path, origin, compat.superseding_record(_local_record(tracker), NEW_EPOCH)
    )
    _git("fetch", "-q", "origin", "tickets", cwd=tracker)
    _mark_recent_read(tracker)
    assert _git_rc("merge-base", "HEAD", "origin/tickets", cwd=tracker) != 0, (
        "precondition: the fetched reclaimed baseline shares no commit with the clone"
    )

    rebar.comment(seed, "written after the reclaim", repo_root=str(repo))

    tip = _remote_tip(origin)
    assert _git_rc("merge-base", "--is-ancestor", baseline, tip, cwd=origin) == 0, (
        "the write must be published on top of the reclaimed baseline"
    )
    added = _git("diff", "--name-only", baseline, tip, cwd=origin).splitlines()
    comments = [path for path in added if path.endswith("-COMMENT.json")]
    assert len(comments) == 1, added
    body = json.loads(_git("show", f"{tip}:{comments[0]}", cwd=origin))
    assert body["data"]["body"] == "written after the reclaim"
    assert _git("rev-parse", "HEAD", cwd=tracker) == tip


def _supersede(tracker: Path, origin: Path, tmp_path: Path) -> str:
    """Publish a baseline that supersedes the clone's epoch, fetch it, and pin the throttle."""
    baseline = _publish_rewritten_baseline(
        tmp_path, origin, compat.superseding_record(_local_record(tracker), NEW_EPOCH)
    )
    _git("fetch", "-q", "origin", "tickets", cwd=tracker)
    _mark_recent_read(tracker)
    return baseline


def _added_events(origin: Path, baseline: str, suffix: str) -> list[str]:
    tip = _remote_tip(origin)
    assert _git_rc("merge-base", "--is-ancestor", baseline, tip, cwd=origin) == 0
    added = _git("diff", "--name-only", baseline, tip, cwd=origin).splitlines()
    return [path for path in added if path.endswith(suffix)]


def test_write_lands_even_when_an_old_unpushed_event_is_discarded(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    """A clone holding an unpushed event older than the reclaim horizon is a STALE clone:
    the override discards that event (kept under a backup ref, ADR 0106 §5). The new write
    must not be swept away with it, because it is composed after the clone has moved."""
    repo, tracker, origin, seed = shared_store
    old_ns = int((time.time() - 90 * 86400) * 1_000_000_000)
    old_rel = f"{seed}/{old_ns}-0000beef-0000-4000-8000-00000000beef-COMMENT.json"
    (tracker / old_rel).write_text('{"old": true}\n', encoding="utf-8")
    _git("add", old_rel, cwd=tracker)
    _git("commit", "-q", "--no-verify", "-m", "unpushed old event", cwd=tracker)
    old_head = _git("rev-parse", "HEAD", cwd=tracker)
    baseline = _supersede(tracker, origin, tmp_path)

    rebar.comment(seed, "the new write survives", repo_root=str(repo))

    comments = _added_events(origin, baseline, "-COMMENT.json")
    assert len(comments) == 1, comments
    body = json.loads(_git("show", f"{_remote_tip(origin)}:{comments[0]}", cwd=origin))
    assert body["data"]["body"] == "the new write survives"
    backups = _git(
        "for-each-ref", "--format=%(objectname)", "refs/rebar/stale-override/", cwd=tracker
    )
    assert backups.splitlines() == [old_head]


def test_transition_on_a_superseded_clone_lands_on_the_reclaimed_remote(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    repo, tracker, origin, seed = shared_store
    baseline = _supersede(tracker, origin, tmp_path)

    rebar.transition(seed, "open", "in_progress", repo_root=str(repo))

    assert len(_added_events(origin, baseline, "-STATUS.json")) == 1


def test_claim_on_a_superseded_clone_lands_on_the_reclaimed_remote(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    repo, tracker, origin, seed = shared_store
    baseline = _supersede(tracker, origin, tmp_path)

    rebar.claim(seed, assignee="t@t.co", repo_root=str(repo))

    assert len(_added_events(origin, baseline, "-STATUS.json")) == 1


def test_write_against_an_unrelated_remote_is_still_refused(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    repo, tracker, origin, seed = shared_store
    record = _local_record(tracker)
    unrelated = {**record, "epoch": NEW_EPOCH, "epoch_lineage": ["some-other-store-epoch"]}
    before_remote = _publish_rewritten_baseline(tmp_path, origin, unrelated)
    _git("fetch", "-q", "origin", "tickets", cwd=tracker)
    _mark_recent_read(tracker)
    before = _git("rev-parse", "HEAD", cwd=tracker)

    with pytest.raises(rebar.RebarError) as exc:
        rebar.comment(seed, "this write must not be stranded", repo_root=str(repo))

    assert "ticket write refused" in str(exc.value)
    assert "DIVERGED" in str(exc.value)
    assert _git("rev-parse", "HEAD", cwd=tracker) == before
    assert _remote_tip(origin) == before_remote
