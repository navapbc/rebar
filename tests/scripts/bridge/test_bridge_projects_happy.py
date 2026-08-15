"""Happy-path oracle for the ``rebar bridge projects`` CLI + library (story c927).

Well-formed set/list through the real CLI parser with a persisted-state assertion, and
the library facade round-trip. The argument-error, empty-list, replace-semantics,
unknown-key, and collateral-invariant cases live in the held-out oracle.
"""

from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import rebar
from rebar._cli._bridge_commands import bridge_cli

pytestmark = [pytest.mark.unit, pytest.mark.scripts]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=r, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    rebar.init_repo(repo_root=str(r))
    return r


def _projects_file(repo: Path) -> Path:
    return repo / ".tickets-tracker" / ".bridge_state" / "projects.json"


def _run(*argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = bridge_cli(list(argv))
    return rc, buf.getvalue()


def test_cli_set_then_list_and_persisted_record(repo: Path) -> None:
    """set writes the entry to projects.json; list prints the projects mapping as JSON."""
    rc_set, _ = _run("projects", "set", "REB", "--repos", "rebar")
    assert rc_set == 0

    record = json.loads(_projects_file(repo).read_text(encoding="utf-8"))
    assert record["projects"]["REB"] == {"repos": ["rebar"]}

    rc_list, out = _run("projects", "list")
    assert rc_list == 0
    assert json.loads(out) == {"REB": {"repos": ["rebar"]}}


def test_library_set_then_list_roundtrip(repo: Path) -> None:
    """The library facade writes and reads back the same entry."""
    rebar.bridge_projects_set("REB", ["rebar"], repo_root=str(repo))

    assert rebar.bridge_projects_list(repo_root=str(repo)) == {"REB": {"repos": ["rebar"]}}


def test_mutation_runs_under_write_lock_and_publishes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tickets-branch invariant regression guard: a projects.json mutation runs its
    read-modify-write UNDER ``lock.write_lock`` and is then published via
    ``push.commit_and_push_tickets_branch`` — both through the shared seams, with the
    tracker path, and with the RMW nested inside the lock (no ad-hoc git)."""
    import contextlib

    from rebar import config
    from rebar._store import lock, push

    events: list[str] = []
    seen_tracker: dict[str, object] = {}

    @contextlib.contextmanager
    def _spy_write_lock(tracker, **kwargs):
        seen_tracker["lock"] = tracker
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    def _spy_commit_and_push(tracker, *, message, **kwargs):
        seen_tracker["push"] = tracker
        seen_tracker["message"] = message
        events.append("commit-push")

    monkeypatch.setattr(lock, "write_lock", _spy_write_lock)
    monkeypatch.setattr(push, "commit_and_push_tickets_branch", _spy_commit_and_push)

    rebar.bridge_projects_set("REB", ["rebar"], repo_root=str(repo))

    tracker = config.tracker_dir(str(repo))
    # The RMW is committed + pushed under the lock via the seam, with the tracker path.
    assert events == ["lock-enter", "lock-exit", "commit-push"]
    assert Path(seen_tracker["lock"]) == tracker
    assert Path(seen_tracker["push"]) == tracker
    assert seen_tracker["message"] == "bridge: update projects mapping"
    # And the RMW actually landed on disk despite the seam being stubbed.
    assert rebar.bridge_projects_list(repo_root=str(repo)) == {"REB": {"repos": ["rebar"]}}


def test_remove_missing_key_does_not_publish(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed mutation (unknown key) must not commit or push — the publish seam is only
    reached after a successful read-modify-write."""
    from rebar import RebarError
    from rebar._store import push

    called = {"n": 0}
    monkeypatch.setattr(
        push,
        "commit_and_push_tickets_branch",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )

    with pytest.raises(RebarError):
        rebar.bridge_projects_remove("NOPE", repo_root=str(repo))

    assert called["n"] == 0
