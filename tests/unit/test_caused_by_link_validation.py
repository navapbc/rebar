"""``caused_by`` link-time validation (story dormant-fibre-pterosaurs).

``rebar link <bug> <target> caused_by`` used to accept ANY target, so the store's only
machine-readable causation edge could point at a ticket that never shipped a line of code.
The rule under test: a ``caused_by`` target must have at least one commit referencing it
(a ``rebar-ticket:`` trailer or a leading ``<id>:`` subject), unless the caller forces.

The tests assert OBSERVABLE behaviour only — the raised error, the recorded ``deps``, the
``--dry-run`` stdout — never a private symbol name or a source spelling.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _bare_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


@pytest.fixture
def repo_with_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store whose CODE repo has real commits, so the scan can reach a verdict."""
    repo = _bare_repo(tmp_path, monkeypatch, "repo")
    _git(repo, "commit", "--allow-empty", "-q", "-m", "root commit")
    return repo


@pytest.fixture
def repo_without_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store whose CODE repo has NO commits: the scan cannot reach a verdict."""
    return _bare_repo(tmp_path, monkeypatch, "empty")


def _relations(tid: str, repo: Path) -> list[tuple[str, str]]:
    return [
        (d["relation"], d["target_id"]) for d in rebar.show_ticket(tid, repo_root=str(repo))["deps"]
    ]


def _culprit_with_commit(repo: Path) -> str:
    """A ticket whose introducing commit carries its ``rebar-ticket:`` trailer."""
    culprit = rebar.create_ticket("task", "the change that broke it", repo_root=str(repo))
    _git(
        repo,
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        f"do the thing\n\nrebar-ticket: {culprit}",
    )
    return culprit


def test_caused_by_to_a_target_with_a_commit_is_recorded(repo_with_history: Path) -> None:
    repo = repo_with_history
    culprit = _culprit_with_commit(repo)
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rebar.link(bug, culprit, "caused_by", repo_root=str(repo))

    assert ("caused_by", culprit) in _relations(bug, repo)


def test_caused_by_to_a_commitless_target_is_refused_and_writes_nothing(
    repo_with_history: Path,
) -> None:
    repo = repo_with_history
    culprit = rebar.create_ticket("task", "never shipped", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    with pytest.raises(rebar.RebarError) as excinfo:
        rebar.link(bug, culprit, "caused_by", repo_root=str(repo))

    message = str(excinfo.value)
    assert culprit in message, f"the refusal must name the target it rejected: {message}"
    assert "--force" in message, f"the refusal must name the escape hatch: {message}"
    assert ("caused_by", culprit) not in _relations(bug, repo), "a refused link left an edge"


def test_force_records_the_edge_the_rule_would_refuse(repo_with_history: Path) -> None:
    repo = repo_with_history
    culprit = rebar.create_ticket("task", "never shipped", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rebar.link(bug, culprit, "caused_by", force="attribution by scope of work", repo_root=str(repo))

    assert ("caused_by", culprit) in _relations(bug, repo)


def test_other_relations_to_the_same_target_are_unaffected(repo_with_history: Path) -> None:
    repo = repo_with_history
    other = rebar.create_ticket("task", "never shipped", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rebar.link(bug, other, "relates_to", repo_root=str(repo))

    assert ("relates_to", other) in _relations(bug, repo)


def test_unreadable_history_allows_the_link(repo_without_history: Path) -> None:
    """A clone with no commits cannot distinguish "no commit" from "no history",
    so the link is allowed: a refusal must never mean "this checkout is empty"."""
    repo = repo_without_history
    culprit = rebar.create_ticket("task", "unverifiable", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rebar.link(bug, culprit, "caused_by", repo_root=str(repo))

    assert ("caused_by", culprit) in _relations(bug, repo)


def test_dry_run_previews_the_refusal_and_writes_nothing(
    repo_with_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from rebar._commands.link_revert import link_cli

    repo = repo_with_history
    culprit = rebar.create_ticket("task", "never shipped", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rc = link_cli([bug, culprit, "caused_by", "--dry-run"], repo_root=str(repo))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Would reject" in out, f"dry run must preview the refusal, not a create: {out}"
    assert culprit in out
    assert ("caused_by", culprit) not in _relations(bug, repo)


def test_dry_run_under_force_previews_the_create(
    repo_with_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from rebar._commands.link_revert import link_cli

    repo = repo_with_history
    culprit = rebar.create_ticket("task", "never shipped", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rc = link_cli([bug, culprit, "caused_by", "--dry-run", "--force=scope"], repo_root=str(repo))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Would create" in out, f"a forced dry run must not preview a refusal: {out}"


def test_close_precheck_scan_still_reports_an_empty_list_when_history_is_unreadable(
    tmp_path: Path,
) -> None:
    """The close gate's contract is ``[]`` for "no referencing commit", including when git
    cannot be read at all — the hoisted scan's ``None`` must not leak into it."""
    from rebar._commands import close_precheck

    assert close_precheck._referencing_commits({"x"}, str(tmp_path), str(tmp_path)) == []
    assert close_precheck._referencing_commit_exists({"x"}, str(tmp_path), str(tmp_path)) is False
