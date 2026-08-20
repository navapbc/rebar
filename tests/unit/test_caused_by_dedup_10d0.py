"""Idempotent ``--caused-by`` on a bug close (bug 10d0 / ruling-magnific-bats).

The ``/rebar-debug`` protocol prescribes BOTH halves of the trigger: record the origin
early with ``rebar link <bug> <origin> caused_by``, then always pass ``--caused-by`` on
the close. Following it faithfully produced two ``caused_by`` edges to the SAME target
(distinct ``link_uuid``s), because the close path writes through the lower-level
``_write_link_event`` — which deliberately bypasses ``add_dependency``'s closed-source
and cycle guards, and with them its ``_is_active_link`` idempotency check.

Pinned here:

* an explicit flag naming the ALREADY-linked origin is a no-op (one edge, not two);
* an explicit flag naming a DIFFERENT origin REPLACES the recorded one (a corrected
  attribution is never silently dropped) — the AC3 decision;
* an EMPTY flag does not let blame add a competing GUESSED edge beside a proven one;
* an EMPTY flag with no existing edge still runs blame — the protection the
  ``/rebar-debug`` guidance exists for must survive this fix.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar

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


def _caused_by_targets(tid: str, repo: str) -> list[str]:
    return [d["target_id"] for d in _caused_by_deps(tid, repo)]


def _open_bug_with_origin(repo: str, *, link: bool = True) -> tuple[str, str]:
    """A bug in ``in_progress`` plus an origin ticket, optionally already linked."""
    origin = rebar.create_ticket("task", "the origin change", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    if link:
        rebar.link(bug, origin, "caused_by", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)
    return bug, origin


def _no_blame(monkeypatch: pytest.MonkeyPatch, culprit: str | None) -> list[bool]:
    """Stub blame auto-derivation; returns a one-slot ledger of whether it was consulted."""
    from rebar.metrics import blame

    consulted: list[bool] = []

    def _fake(*_args, **_kwargs):
        consulted.append(True)
        return culprit

    monkeypatch.setattr(blame, "derive_caused_by", _fake)
    return consulted


def test_explicit_flag_matching_existing_link_leaves_one_edge(rebar_repo) -> None:
    """The four-step reproduction: link, then close with the same origin -> ONE edge.

    A true NO-OP, not a churn: the recorded link is left in place, so its ``link_uuid``
    (and with it the timestamp of the proven attribution) survives the close untouched.
    """
    repo = str(rebar_repo)
    bug, origin = _open_bug_with_origin(repo)
    recorded = _caused_by_deps(bug, repo)
    assert [d["target_id"] for d in recorded] == [origin]

    rebar.transition(
        bug, "in_progress", "closed", close_class="regression", caused_by=origin, repo_root=repo
    )

    after = _caused_by_deps(bug, repo)
    assert [d["target_id"] for d in after] == [origin]
    assert [d["link_uuid"] for d in after] == [recorded[0]["link_uuid"]], (
        "the close must leave the existing edge alone, not rewrite it"
    )


def test_explicit_flag_with_different_origin_replaces_the_recorded_one(rebar_repo) -> None:
    """AC3: a CORRECTED attribution wins outright — it is never silently dropped."""
    repo = str(rebar_repo)
    bug, _wrong_origin = _open_bug_with_origin(repo)
    real_origin = rebar.create_ticket("task", "the real origin", repo_root=repo)

    rebar.transition(
        bug,
        "in_progress",
        "closed",
        close_class="regression",
        caused_by=real_origin,
        repo_root=repo,
    )

    assert _caused_by_targets(bug, repo) == [real_origin]


def test_empty_flag_does_not_add_a_competing_guessed_edge(
    rebar_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proven edge already on the ticket is not joined by a blame GUESS."""
    repo = str(rebar_repo)
    bug, origin = _open_bug_with_origin(repo)
    wrong_guess = rebar.create_ticket("task", "innocent carrier commit", repo_root=repo)
    _no_blame(monkeypatch, wrong_guess)

    rebar.transition(bug, "in_progress", "closed", close_class="regression", repo_root=repo)

    assert _caused_by_targets(bug, repo) == [origin]


def test_empty_flag_still_autoderives_when_no_edge_exists(
    rebar_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANTI-REGRESSION: blame auto-derivation is untouched on a bug with no caused_by edge."""
    repo = str(rebar_repo)
    bug, origin = _open_bug_with_origin(repo, link=False)
    consulted = _no_blame(monkeypatch, origin)

    rebar.transition(bug, "in_progress", "closed", close_class="regression", repo_root=repo)

    assert consulted == [True], "blame must still run when the bug carries no caused_by edge"
    assert _caused_by_targets(bug, repo) == [origin]


def test_escape_metric_counts_the_link_then_flag_bug_once(rebar_repo) -> None:
    """AC4: the escape-rate lens tallies EDGES, so a duplicate double-charged one origin."""
    from rebar.metrics.bug_trends import caused_by_fan_in

    repo = str(rebar_repo)
    bug, origin = _open_bug_with_origin(repo)
    rebar.transition(
        bug, "in_progress", "closed", close_class="regression", caused_by=origin, repo_root=repo
    )

    assert caused_by_fan_in(repo) == {origin: 1}
