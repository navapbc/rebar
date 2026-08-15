"""Inline-commit writes auto-push on their own (bug prone-octet-cheek).

``transition`` / ``reopen`` / ``claim`` (txn.py), ``compact`` (compact.py), and
``delete`` (delete.py) do their own locked rename+commit instead of going through
``write_and_push``. The auto-push must still fire for each, otherwise a trailing
status/compact/delete — the LAST write of a session, e.g. closing an epic — leaves
its commit stranded as PUSH_PENDING (origin/tickets behind local).

These pin the observable git effect against a real local bare origin: after each
such write, with NO following append_event write to "carry" it, the local tickets
branch must be EVEN with origin/tickets (ahead == 0). The default push policy
(``always``) is in force (the push-policy matrix is covered by
test_push_policy_e2e.py).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from _topology_template import clone_topology_template

import rebar
from rebar._store import push


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _build_repo_with_origin(root: Path) -> Path:
    origin = root / "origin.git"
    repo = root / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True, text=True
    )
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t.co", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    rebar.init_repo(repo_root=str(repo))
    return repo


@pytest.fixture(scope="session")
def _repo_with_origin_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("inline-origin-template")
    repo = root / "work"
    from rebar import config as _config

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("REBAR_ROOT", str(repo))
        patch.setenv("XDG_CONFIG_HOME", str(root / "xdg-empty"))
        for variable in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
            patch.delenv(variable, raising=False)
        _config.reset_config_cache()
        try:
            _build_repo_with_origin(root)
        finally:
            _config.reset_config_cache()
    return root


@pytest.fixture
def repo_with_origin(
    _repo_with_origin_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    root = clone_topology_template(_repo_with_origin_template, tmp_path / "inline-origin")
    repo = root / "work"
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "gate"))
    from rebar import config as _config
    from rebar._store import ensures as _ensures

    _config.reset_config_cache()
    _ensures._reset_pending_cache()
    try:
        yield repo
    finally:
        _config.reset_config_cache()
        _ensures._reset_pending_cache()


def _ahead(repo: Path) -> int:
    """How many commits the local tickets branch is ahead of origin/tickets."""
    tracker = repo / ".tickets-tracker"
    subprocess.run(
        ["git", "fetch", "-q", "origin", "tickets"], cwd=tracker, capture_output=True, text=True
    )
    out = subprocess.run(
        ["git", "rev-list", "--count", "FETCH_HEAD..HEAD"],
        cwd=tracker,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip() or "0")


def _ac(n: str) -> str:
    return f"Body for {n}.\n\n## Acceptance Criteria\n- [ ] x"


def test_claim_pushes_on_its_own(repo_with_origin: Path) -> None:
    repo = repo_with_origin
    t = rebar.create_ticket("task", "T", description=_ac("T"), repo_root=str(repo))
    assert _ahead(repo) == 0  # create pushed
    rebar.claim(t, assignee="me", repo_root=str(repo))
    assert _ahead(repo) == 0, "claim's STATUS commit must reach origin without a carrying write"


def test_transition_close_pushes_on_its_own(repo_with_origin: Path) -> None:
    repo = repo_with_origin
    t = rebar.create_ticket("task", "T", description=_ac("T"), repo_root=str(repo))
    rebar.claim(t, assignee="me", repo_root=str(repo))
    rebar.transition(t, "in_progress", "closed", repo_root=str(repo))
    # close writes STATUS + a compact-on-close SNAPSHOT, both inline-committed.
    assert _ahead(repo) == 0, "a trailing close must not strand its STATUS/SNAPSHOT (PUSH_PENDING)"


def test_reopen_pushes_on_its_own(repo_with_origin: Path) -> None:
    repo = repo_with_origin
    t = rebar.create_ticket("task", "T", description=_ac("T"), repo_root=str(repo))
    rebar.transition(t, "open", "closed", repo_root=str(repo))
    rebar.reopen(t, repo_root=str(repo))
    assert _ahead(repo) == 0, "reopen (a transition) must push on its own"


def test_compact_pushes_on_its_own(repo_with_origin: Path) -> None:
    repo = repo_with_origin
    from rebar._commands import compact

    t = rebar.create_ticket("task", "T", description=_ac("T"), repo_root=str(repo))
    for i in range(3):
        rebar.comment(t, f"comment {i}", repo_root=str(repo))
    assert _ahead(repo) == 0
    rc = compact.compact_cli([t, "--threshold=0"], repo_root=str(repo))
    assert rc == 0
    assert _ahead(repo) == 0, "a standalone compact's SNAPSHOT commit must reach origin"


def test_delete_pushes_on_its_own(repo_with_origin: Path) -> None:
    repo = repo_with_origin
    from rebar._commands import delete

    t = rebar.create_ticket("task", "T", description=_ac("T"), repo_root=str(repo))
    assert _ahead(repo) == 0
    rc = delete.delete_cli([t, "--user-approved"], repo_root=str(repo))
    assert rc == 0
    assert _ahead(repo) == 0, "a trailing delete must not strand its DELETE commit"


@pytest.mark.parametrize("operation", ["claim", "close", "compact", "delete"])
def test_inline_write_callers_keep_the_legacy_best_effort_push_contract(
    repo_with_origin: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Each shipped caller reaches push_after_commit without opting into strict mode."""
    repo = repo_with_origin
    ticket = rebar.create_ticket("task", "T", description=_ac("T"), repo_root=str(repo))
    if operation == "close":
        rebar.claim(ticket, assignee="me", repo_root=str(repo))
    if operation == "compact":
        for index in range(3):
            rebar.comment(ticket, f"comment {index}", repo_root=str(repo))

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        push,
        "push_after_commit",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    if operation == "claim":
        rebar.claim(ticket, assignee="me", repo_root=str(repo))
    elif operation == "close":
        rebar.transition(ticket, "in_progress", "closed", repo_root=str(repo))
    elif operation == "compact":
        from rebar._commands import compact

        assert compact.compact_cli([ticket, "--threshold=0"], repo_root=str(repo)) == 0
    else:
        from rebar._commands import delete

        assert delete.delete_cli([ticket, "--user-approved"], repo_root=str(repo)) == 0

    assert calls
    assert all(kwargs == {} for _args, kwargs in calls)


def test_compact_all_keeps_the_legacy_best_effort_push_contract(
    repo_with_origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands import compact

    repo = repo_with_origin
    ticket = rebar.create_ticket("task", "T", description=_ac("T"), repo_root=str(repo))
    rebar.comment(ticket, "one", repo_root=str(repo))
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        push,
        "push_after_commit",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert compact.compact_all_cli([], repo_root=str(repo)) == 0
    assert calls and all(kwargs == {} for _args, kwargs in calls)
