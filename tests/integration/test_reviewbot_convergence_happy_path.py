"""Happy-path oracle for the review-bot persistent-clone epoch adoption."""

from __future__ import annotations

import json

import pytest
from reviewbot_convergence_support import (
    assert_no_merge_or_stash,
    force_epoch_rewrite,
    git,
    make_store_pair,
    rev,
    run_ensure,
)

pytestmark = pytest.mark.integration


def test_clean_pre_epoch_clone_adopts_rewritten_epoch_tip(tmp_path, monkeypatch) -> None:
    pair = make_store_pair(tmp_path, monkeypatch)
    local_before = rev(pair.clone)
    remote_after = force_epoch_rewrite(pair, "2026-08-14T09-31-07Z-4f2a")

    assert local_before == pair.old_tip
    assert (
        not git(
            pair.clone, "merge-base", "--is-ancestor", local_before, remote_after, check=False
        ).returncode
        == 0
    )

    result = run_ensure(pair)

    assert result.returncode == 0, result.stderr
    assert rev(pair.clone) == remote_after
    assert rev(pair.clone, "refs/remotes/origin/tickets") == remote_after
    record = json.loads(git(pair.clone, "show", "HEAD:.store-compat.json").stdout)
    assert record["epoch"] == "2026-08-14T09-31-07Z-4f2a"
    assert local_before in result.stderr
    assert remote_after in result.stderr
    assert "adopt" in result.stderr.lower()
    assert_no_merge_or_stash(pair.clone)
