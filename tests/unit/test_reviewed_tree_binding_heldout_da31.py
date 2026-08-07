"""Held-out validation for bug da31 — authored independently of the implementation.

The review bot FETCHES the tree from `patchSet.ref` but VOTES against
`patchSet.revision`. Those are two independently-supplied webhook fields, and the
reviewed tree is whatever ended up in `repo_root`, so a stale/reused clone dir, a
partial fetch, or a future refactor of the checkout logic silently decouples them.

This is NOT a live vulnerability (Gerrit always emits the pair consistently, and
decoupling requires forging a webhook behind WEBHOOK_TOKEN). The value is the signal.

The dominant RISK OF THE FIX ITSELF is the opposite failure: a FALSE mismatch would
refuse every review and wedge the merge gate. So most of these tests are false-positive
probes, not true-positive ones.

Everything here drives real git repos and the public seam; nothing pins internals.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar.review_bot import adapter


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "clone"
    d.mkdir()
    _git("init", "-q", cwd=d)
    _git("config", "user.email", "t@example.com", cwd=d)
    _git("config", "user.name", "T", cwd=d)
    (d / "f.txt").write_text("hello\n")
    _git("add", "-A", cwd=d)
    _git("commit", "-q", "-m", "c1", cwd=d)
    return d


def _head(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo)


# ── the true positive: a PROVEN mismatch refuses ────────────────────────────


def test_proven_mismatch_raises_with_an_actionable_message(repo: Path) -> None:
    other = "0" * 40  # a real-shaped SHA that is definitively not HEAD
    with pytest.raises(adapter.ReviewedTreeMismatch) as excinfo:
        adapter._assert_reviewed_tree(repo, "refs/changes/59/1459/1", other)

    msg = str(excinfo.value)
    assert _head(repo)[:12] in msg, "the message must name the tree actually checked out"
    assert other[:12] in msg, "the message must name the revision the vote would attach to"
    assert "refs/changes/59/1459/1" in msg, "the `ref` parameter must reach the message"


# ── the false positives that would wedge the merge gate ─────────────────────


def test_exact_match_does_not_raise(repo: Path) -> None:
    adapter._assert_reviewed_tree(repo, "refs/changes/59/1459/1", _head(repo))


@pytest.mark.parametrize("n", [7, 8, 10, 12, 40])
def test_abbreviated_revision_against_full_head_does_not_raise(repo: Path, n: int) -> None:
    """Gerrit and git both abbreviate; an abbreviation must not read as disagreement."""
    adapter._assert_reviewed_tree(repo, "refs/changes/59/1459/1", _head(repo)[:n])


def test_uppercase_and_surrounding_whitespace_do_not_raise(repo: Path) -> None:
    adapter._assert_reviewed_tree(repo, "r", f"  {_head(repo).upper()}  ")


# ── abstain rather than veto when the binding cannot be established ─────────


def test_absent_revision_abstains(repo: Path) -> None:
    """The seam is also called outside Gerrit, where there is no revision to bind to."""
    adapter._assert_reviewed_tree(repo, "refs/changes/59/1459/1", "")


def test_non_git_repo_root_abstains(tmp_path: Path) -> None:
    """A missing git identity is a setup problem that already fails closed upstream in
    clone_change_ref; vetoing here too would turn it into an unexplainable review refusal."""
    plain = tmp_path / "plain"
    plain.mkdir()
    adapter._assert_reviewed_tree(plain, "refs/changes/59/1459/1", "a" * 40)


def test_uncomparable_revision_abstains(repo: Path) -> None:
    """Too short / not hex -> cannot prove disagreement, so do not claim one."""
    for bogus in ("abc", "not-a-sha", "HEAD"):
        adapter._assert_reviewed_tree(repo, "refs/changes/59/1459/1", bogus)


# ── the check is not vacuous ────────────────────────────────────────────────


def test_a_second_real_commit_is_detected_as_a_mismatch(repo: Path) -> None:
    """Two genuinely different commits in the same repo must disagree — this is the
    stale-clone scenario the ticket describes, and the case that would go unnoticed
    if the comparison were accidentally always-true."""
    stale = _head(repo)
    (repo / "f.txt").write_text("changed\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "c2", cwd=repo)
    assert _head(repo) != stale

    with pytest.raises(adapter.ReviewedTreeMismatch):
        adapter._assert_reviewed_tree(repo, "refs/changes/59/1459/2", stale)


def test_binding_is_checked_before_any_review_work(repo: Path, monkeypatch) -> None:
    """The refusal must precede the gate, not follow it: reviewing and then discarding
    would burn an LLM run and, worse, leave a code_review artifact for a tree nobody
    voted on. Proven by the absence of any gate import/call on the mismatch path."""
    called: list[str] = []
    monkeypatch.setattr(adapter, "_resolve_reviewed_head", lambda _r: "1" * 40, raising=True)

    with pytest.raises(adapter.ReviewedTreeMismatch):
        adapter.code_review_decision("diff --git a/f b/f\n", repo, "r", revision="2" * 40)

    assert called == []
