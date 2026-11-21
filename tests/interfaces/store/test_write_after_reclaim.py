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
import time
from pathlib import Path

import pytest
from reclaim_harness import (
    added_events,
    build_shared_store,
    git,
    publish_unrelated,
    remote_tip,
    supersede,
)

import rebar


@pytest.fixture
def shared_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, str]:
    return build_shared_store(tmp_path, monkeypatch)


def test_write_on_a_superseded_clone_lands_on_the_reclaimed_remote(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    repo, tracker, origin, seed = shared_store
    baseline = supersede(tracker, origin, tmp_path)

    rebar.comment(seed, "written after the reclaim", repo_root=str(repo))

    comments = added_events(origin, baseline, "-COMMENT.json")
    assert len(comments) == 1, comments
    tip = remote_tip(origin)
    body = json.loads(git("show", f"{tip}:{comments[0]}", cwd=origin))
    assert body["data"]["body"] == "written after the reclaim"
    assert git("rev-parse", "HEAD", cwd=tracker) == tip


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
    git("add", old_rel, cwd=tracker)
    git("commit", "-q", "--no-verify", "-m", "unpushed old event", cwd=tracker)
    old_head = git("rev-parse", "HEAD", cwd=tracker)
    baseline = supersede(tracker, origin, tmp_path)

    rebar.comment(seed, "the new write survives", repo_root=str(repo))

    comments = added_events(origin, baseline, "-COMMENT.json")
    assert len(comments) == 1, comments
    body = json.loads(git("show", f"{remote_tip(origin)}:{comments[0]}", cwd=origin))
    assert body["data"]["body"] == "the new write survives"
    backups = git(
        "for-each-ref", "--format=%(objectname)", "refs/rebar/stale-override/", cwd=tracker
    )
    assert backups.splitlines() == [old_head]


def test_transition_on_a_superseded_clone_lands_on_the_reclaimed_remote(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    repo, tracker, origin, seed = shared_store
    baseline = supersede(tracker, origin, tmp_path)

    rebar.transition(seed, "open", "in_progress", repo_root=str(repo))

    assert len(added_events(origin, baseline, "-STATUS.json")) == 1


def test_claim_on_a_superseded_clone_lands_on_the_reclaimed_remote(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    repo, tracker, origin, seed = shared_store
    baseline = supersede(tracker, origin, tmp_path)

    rebar.claim(seed, assignee="t@t.co", repo_root=str(repo))

    assert len(added_events(origin, baseline, "-STATUS.json")) == 1


def test_write_against_an_unrelated_remote_is_still_refused(
    shared_store: tuple[Path, Path, Path, str], tmp_path: Path
) -> None:
    repo, tracker, origin, seed = shared_store
    before_remote = publish_unrelated(tracker, origin, tmp_path)
    before = git("rev-parse", "HEAD", cwd=tracker)

    with pytest.raises(rebar.RebarError) as exc:
        rebar.comment(seed, "this write must not be stranded", repo_root=str(repo))

    assert "ticket write refused" in str(exc.value)
    assert "DIVERGED" in str(exc.value)
    assert git("rev-parse", "HEAD", cwd=tracker) == before
    assert remote_tip(origin) == before_remote
