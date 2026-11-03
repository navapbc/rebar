"""A close's explicit ``--caused-by`` must name exactly one ticket (surprising-fruticose-herring).

Before the fix, an ambiguous or unknown value fell back to the raw string: the close removed
the bug's existing ``caused_by`` edges and wrote one whose target was that string, so the edge
pointed at no ticket. The close now refuses before writing anything, naming the candidates, the
way ``rebar link`` refuses an unresolved target.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import RebarError, config
from rebar._commands import transition_close

pytestmark = pytest.mark.unit


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _caused_by_deps(tid: str, repo: str) -> list[dict]:
    deps = rebar.show_ticket(tid, repo_root=repo)["deps"]
    return [d for d in deps if d["relation"] == "caused_by"]


def _linked_bug(repo: str) -> tuple[str, str]:
    """An ``in_progress`` bug with a valid ``caused_by`` edge to a task."""
    origin = rebar.create_ticket("task", "the origin change", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    rebar.link(bug, origin, "caused_by", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)
    return bug, origin


def _twin_sharing_first_quad(repo: str, origin: str) -> str:
    """A second ticket whose first quad equals ``origin``'s, making that quad ambiguous."""
    tracker = str(config.tracker_dir(repo))
    twin = rebar.create_ticket("task", "unrelated twin", repo_root=repo)
    twin_id = f"{origin[:4]}-{twin[5:]}"
    os.rename(os.path.join(tracker, twin), os.path.join(tracker, twin_id))
    return twin_id


def _assert_nothing_written(bug: str, repo: str, before: list[dict]) -> None:
    assert rebar.show_ticket(bug, repo_root=repo)["status"] == "in_progress"
    after = _caused_by_deps(bug, repo)
    assert [(d["target_id"], d["link_uuid"]) for d in after] == [
        (d["target_id"], d["link_uuid"]) for d in before
    ], "the existing caused_by edge must survive untouched"


def test_ambiguous_4hex_caused_by_is_refused_and_names_candidates(rebar_repo) -> None:
    repo = str(rebar_repo)
    bug, origin = _linked_bug(repo)
    twin = _twin_sharing_first_quad(repo, origin)
    before = _caused_by_deps(bug, repo)

    with pytest.raises(RebarError) as excinfo:
        rebar.transition(
            bug,
            "in_progress",
            "closed",
            close_class="regression",
            caused_by=origin[:4],
            repo_root=repo,
        )

    message = str(excinfo.value)
    assert origin in message and twin in message
    assert excinfo.value.returncode == 1
    _assert_nothing_written(bug, repo, before)


def test_unknown_caused_by_is_refused(rebar_repo) -> None:
    repo = str(rebar_repo)
    bug, _origin = _linked_bug(repo)
    before = _caused_by_deps(bug, repo)

    with pytest.raises(RebarError) as excinfo:
        rebar.transition(
            bug,
            "in_progress",
            "closed",
            close_class="regression",
            caused_by="ffff-0000-nope-0000",
            repo_root=repo,
        )

    assert "no ticket matches" in str(excinfo.value)
    _assert_nothing_written(bug, repo, before)


def test_unique_short_caused_by_records_the_canonical_id(rebar_repo) -> None:
    repo = str(rebar_repo)
    bug, _wrong = _linked_bug(repo)
    real_origin = rebar.create_ticket("task", "the real origin", repo_root=repo)

    rebar.transition(
        bug,
        "in_progress",
        "closed",
        close_class="regression",
        caused_by=real_origin[:9],
        repo_root=repo,
    )

    assert [d["target_id"] for d in _caused_by_deps(bug, repo)] == [real_origin]


def test_blank_caused_by_is_not_validated(rebar_repo) -> None:
    """The import path calls ``transition_compute`` with no ``caused_by``; blank passes."""
    tracker = str(config.tracker_dir(str(rebar_repo)))
    assert transition_close.resolve_explicit_caused_by("", "closed", tracker) == ""
    assert transition_close.resolve_explicit_caused_by("  ", "closed", tracker) == ""
    assert transition_close.resolve_explicit_caused_by("ffff", "in_progress", tracker) == ""


def test_culprit_resolver_never_returns_the_raw_value(rebar_repo) -> None:
    """Defense in depth for direct ``close_ticket`` callers that skip the pre-write check."""
    repo = str(rebar_repo)
    bug, origin = _linked_bug(repo)
    _twin_sharing_first_quad(repo, origin)
    tracker = str(config.tracker_dir(repo))

    culprit = transition_close._resolve_caused_by_culprit(origin[:4], [origin], bug, tracker, repo)

    assert culprit is None
