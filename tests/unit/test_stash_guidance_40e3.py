from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_agents_bans_git_stash_repo_wide_and_points_to_concurrency_docs() -> None:
    text = (ROOT / "AGENTS.md").read_text()

    assert "do not use\n  `git stash` anywhere in this repository" in text
    assert "linked worktrees share one repo-global\n  stash stack" in text
    assert "see `docs/concurrency.md`" in text


def test_concurrency_docs_name_untracked_stash_create_caveat_and_oracle_move() -> None:
    text = (ROOT / "docs/concurrency.md").read_text()

    assert "safe for the tracked-file ticket-store\nrecovery path" in text
    assert "`git stash create` does\nnot capture untracked files" in text
    assert "$(git rev-parse --git-path heldout)" in text


def test_push_recovery_documents_tracked_only_audit() -> None:
    text = (ROOT / "src/rebar/_store/push_recovery.py").read_text()

    assert "only sets aside tracked modifications" in text
    assert "untracked files remain in the\n    working tree" in text
    assert "not a held-out-oracle\n    substitute for untracked tests" in text
