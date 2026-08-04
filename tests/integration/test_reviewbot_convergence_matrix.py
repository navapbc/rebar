"""Held-out error, boundary, and negative-control oracle for A6-0b."""

from __future__ import annotations

import json
import os
import shutil
import sys

import pytest
from reviewbot_convergence_support import (
    assert_no_merge_or_stash,
    commit_epoch_on_origin,
    commit_local_file,
    commit_on_origin,
    force_epoch_rewrite,
    git,
    is_ancestor,
    make_store_pair,
    rev,
    run_ensure,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("state", ["equal", "behind", "ahead"])
def test_compatible_linear_histories_keep_the_exact_safe_tip(state, tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    remote_tip = pair.old_tip
    expected_head = pair.old_tip

    if state == "behind":
        remote_tip = commit_on_origin(pair, "remote-event.json", "remote\n", "remote event")
        assert is_ancestor(pair.origin, pair.old_tip, remote_tip)
        expected_head = remote_tip
    elif state == "ahead":
        expected_head = commit_local_file(pair.clone, "local-event.json", "local\n", "local event")
        assert is_ancestor(pair.clone, remote_tip, expected_head)
    else:
        assert rev(pair.clone) == remote_tip

    result = run_ensure(pair)

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == expected_head
    assert rev(pair.clone, "refs/remotes/origin/tickets") == remote_tip
    assert_no_merge_or_stash(pair.clone)


def test_same_epoch_divergence_uses_union_and_preserves_both_event_commits(
    tmp_path, monkeypatch
) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    local_event = commit_local_file(pair.clone, "local-event.json", "local\n", "local event")
    remote_event = commit_on_origin(pair, "remote-event.json", "remote\n", "remote event")

    result = run_ensure(pair)

    assert result.returncode == 0, result.stderr
    assert is_ancestor(pair.clone, local_event, "HEAD")
    assert is_ancestor(pair.clone, remote_event, "HEAD")
    assert (pair.clone / "local-event.json").read_text() == "local\n"
    assert (pair.clone / "remote-event.json").read_text() == "remote\n"
    assert_no_merge_or_stash(pair.clone)


def test_local_ahead_before_epoch_rewrite_is_preserved_and_requires_manual_intervention(
    tmp_path, monkeypatch
) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    local_event = commit_local_file(pair.clone, "unpushed.json", "keep me\n", "unpushed event")
    rewritten = force_epoch_rewrite(pair, "E1")

    result = run_ensure(pair)

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == local_event
    assert (pair.clone / "unpushed.json").read_text() == "keep me\n"
    assert rev(pair.clone, "refs/remotes/origin/tickets") == rewritten
    assert "manual" in result.stderr.lower()
    assert "local" in result.stderr.lower()
    assert_no_merge_or_stash(pair.clone)


def test_local_divergence_before_epoch_rewrite_is_preserved_and_requires_manual_intervention(
    tmp_path, monkeypatch
) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    prior_remote = commit_on_origin(pair, "remote-event.json", "remote\n", "remote event")
    git(
        pair.clone,
        "fetch",
        "origin",
        "+refs/heads/tickets:refs/remotes/origin/tickets",
    )
    local_event = commit_local_file(pair.clone, "local-event.json", "local\n", "local event")
    assert not is_ancestor(pair.clone, local_event, prior_remote)
    assert not is_ancestor(pair.clone, prior_remote, local_event)
    rewritten = force_epoch_rewrite(pair, "E1", source_tip=prior_remote)

    result = run_ensure(pair)

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == local_event
    assert rev(pair.clone, "refs/remotes/origin/tickets") == rewritten
    assert "manual" in result.stderr.lower()
    assert "diverged" in result.stderr.lower() or "local" in result.stderr.lower()
    assert_no_merge_or_stash(pair.clone)


def test_missing_prior_remote_ref_refuses_epoch_adoption_without_moving_head(
    tmp_path, monkeypatch
) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    local_before = rev(pair.clone)
    git(pair.clone, "update-ref", "-d", "refs/remotes/origin/tickets")
    force_epoch_rewrite(pair, "E1")

    result = run_ensure(pair)

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == local_before
    assert "prior" in result.stderr.lower()
    assert "manual" in result.stderr.lower()
    assert_no_merge_or_stash(pair.clone)


def test_corrupt_remote_epoch_refuses_adoption_and_preserves_head(tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    local_before = rev(pair.clone)
    force_epoch_rewrite(pair, 7)

    result = run_ensure(pair)

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == local_before
    assert "malformed" in result.stderr.lower() or "corrupt" in result.stderr.lower()
    assert "manual" in result.stderr.lower()
    assert_no_merge_or_stash(pair.clone)


def test_corrupt_local_epoch_refuses_adoption_and_preserves_head(tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    record = json.loads(git(pair.clone, "show", "HEAD:.store-compat.json").stdout)
    record["epoch"] = 7
    local_before = commit_local_file(
        pair.clone,
        ".store-compat.json",
        json.dumps(record, sort_keys=True) + "\n",
        "corrupt local epoch",
    )
    force_epoch_rewrite(pair, "E1")

    result = run_ensure(pair)

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == local_before
    assert "malformed" in result.stderr.lower() or "corrupt" in result.stderr.lower()
    assert "manual" in result.stderr.lower()
    assert_no_merge_or_stash(pair.clone)


def test_existing_epoch_mismatch_is_not_treated_as_first_reclaim(tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    e0_tip = commit_epoch_on_origin(pair, "E0")
    git(
        pair.clone,
        "fetch",
        "origin",
        "+refs/heads/tickets:refs/remotes/origin/tickets",
    )
    git(pair.clone, "reset", "--hard", e0_tip)
    rewritten = force_epoch_rewrite(pair, "E1", source_tip=e0_tip)

    result = run_ensure(pair)

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == e0_tip
    assert rev(pair.clone, "refs/remotes/origin/tickets") == rewritten
    assert "manual" in result.stderr.lower()
    assert_no_merge_or_stash(pair.clone)


def test_fetch_failure_is_best_effort_and_preserves_exact_tip(tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    local_before = commit_local_file(pair.clone, "pending.json", "pending\n", "pending")
    git(pair.clone, "remote", "set-url", "origin", str(tmp_path / "missing-origin"))

    result = run_ensure(pair)

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == local_before
    assert "fetch" in result.stderr.lower()
    assert "deferred" in result.stderr.lower() or "unavailable" in result.stderr.lower()
    assert_no_merge_or_stash(pair.clone)


def test_epoch_adoption_uses_pinned_commit_when_tracking_ref_moves(tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    validated = force_epoch_rewrite(pair, "E1")
    moved = commit_on_origin(pair, "later-event.json", "later\n", "later remote event")
    git(pair.clone, "fetch", "origin", moved)
    assert rev(pair.clone, "FETCH_HEAD") == moved
    git(pair.origin, "update-ref", "refs/heads/tickets", validated, moved)
    real_git = shutil.which("git")
    assert real_git is not None
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"reset --hard"*)\n'
        f'    "{real_git}" -C "{pair.clone}" update-ref refs/remotes/origin/tickets {moved}\n'
        "    ;;\n"
        "esac\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)

    result = run_ensure(pair, env_overrides={"PATH": f"{shim_dir}:{os.environ['PATH']}"})

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == validated
    assert rev(pair.clone, "refs/remotes/origin/tickets") == moved
    assert_no_merge_or_stash(pair.clone)


def test_under_lock_corrupt_epoch_recheck_refuses_reset(tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    local_before = rev(pair.clone)
    remote_after = force_epoch_rewrite(pair, "E1")
    real_git = shutil.which("git")
    assert real_git is not None
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    compat_record = pair.clone / ".store-compat.json"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"diff --cached --quiet"*)\n'
        f'    "{real_git}" "$@"\n'
        "    status=$?\n"
        f'    printf "not-json\\n" > "{compat_record}"\n'
        '    exit "$status"\n'
        "    ;;\n"
        "esac\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)

    result = run_ensure(pair, env_overrides={"PATH": f"{shim_dir}:{os.environ['PATH']}"})

    assert rev(pair.clone) == local_before
    assert rev(pair.clone) != remote_after
    assert "manual" in result.stderr.lower()
    assert "valid json" in result.stderr.lower() or "corrupt" in result.stderr.lower()
    assert_no_merge_or_stash(pair.clone)


def test_under_lock_dirty_worktree_recheck_refuses_reset(tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    local_before = rev(pair.clone)
    remote_after = force_epoch_rewrite(pair, "E1")
    real_git = shutil.which("git")
    assert real_git is not None
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    compat_record = pair.clone / ".store-compat.json"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"diff --quiet"*)\n'
        f'    printf "\\n" >> "{compat_record}"\n'
        "    ;;\n"
        "esac\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)

    result = run_ensure(pair, env_overrides={"PATH": f"{shim_dir}:{os.environ['PATH']}"})

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == local_before
    assert rev(pair.clone) != remote_after
    assert "manual" in result.stderr.lower()
    assert "uncommitted" in result.stderr.lower()
    assert_no_merge_or_stash(pair.clone)


def test_under_lock_head_change_recheck_preserves_new_local_commit(tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    local_before = rev(pair.clone)
    concurrent_local = commit_local_file(
        pair.clone, "concurrent-event.json", "local\n", "concurrent local event"
    )
    git(pair.clone, "reset", "--hard", local_before)
    remote_after = force_epoch_rewrite(pair, "E1")
    real_git = shutil.which("git")
    assert real_git is not None
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    calls = tmp_path / "head-rev-parse-calls"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"rev-parse --verify HEAD^{commit}"*)\n'
        "    count=0\n"
        f'    if [ -f "{calls}" ]; then count=$(wc -l < "{calls}"); fi\n'
        f'    echo call >> "{calls}"\n'
        '    if [ "$count" -eq 1 ]; then\n'
        f'      "{real_git}" -C "{pair.clone}" update-ref refs/heads/tickets {concurrent_local}\n'
        "    fi\n"
        "    ;;\n"
        "esac\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)

    result = run_ensure(pair, env_overrides={"PATH": f"{shim_dir}:{os.environ['PATH']}"})

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == concurrent_local
    assert rev(pair.clone) != remote_after
    assert "manual" in result.stderr.lower()
    assert "changed while waiting" in result.stderr.lower()
    assert_no_merge_or_stash(pair.clone)


def test_unexpected_convergence_failure_still_runs_ensure_registry(tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    local_before = rev(pair.clone)
    remote_after = force_epoch_rewrite(pair, "E1")
    ensure_receipt = tmp_path / "ensure-ran"
    python_shim = tmp_path / "python-shim"
    python_shim.write_text(
        "#!/bin/sh\n"
        'if [ "$#" -gt 2 ]; then\n'
        '  echo "injected convergence interpreter failure" >&2\n'
        "  exit 42\n"
        "fi\n"
        f'echo ensure >> "{ensure_receipt}"\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    python_shim.chmod(0o755)

    result = run_ensure(pair, env_overrides={"REVIEWBOT_PYTHON": str(python_shim)})

    assert result.returncode == 0, result.stderr
    assert ensure_receipt.read_text().splitlines() == ["ensure"]
    assert rev(pair.clone) == local_before
    assert rev(pair.clone, "refs/remotes/origin/tickets") == remote_after
    assert "convergence" in result.stderr.lower()
    assert "deferred" in result.stderr.lower() or "failed" in result.stderr.lower()


def test_under_lock_rebase_guard_refuses_reset(tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    local_before = rev(pair.clone)
    remote_after = force_epoch_rewrite(pair, "E1")
    merge_head = pair.clone / ".git" / "MERGE_HEAD"
    merge_head.write_text(remote_after + "\n")

    result = run_ensure(pair)

    assert rev(pair.clone) == local_before
    assert rev(pair.clone) != remote_after
    assert merge_head.exists()
    assert "manual" in result.stderr.lower()
    assert "merge_head" in result.stderr.lower()


def test_failed_pinned_reset_warns_and_preserves_head(tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    local_before = rev(pair.clone)
    remote_after = force_epoch_rewrite(pair, "E1")
    real_git = shutil.which("git")
    assert real_git is not None
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        f'#!/bin/sh\ncase "$*" in\n  *"reset --hard"*) exit 1 ;;\nesac\nexec "{real_git}" "$@"\n'
    )
    shim.chmod(0o755)

    result = run_ensure(pair, env_overrides={"PATH": f"{shim_dir}:{os.environ['PATH']}"})

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == local_before
    assert rev(pair.clone) != remote_after
    assert "manual" in result.stderr.lower()
    assert "could not adopt" in result.stderr.lower()
    assert "adopted pre-epoch" not in result.stderr.lower()
    assert_no_merge_or_stash(pair.clone)
