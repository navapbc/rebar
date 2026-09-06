"""``rebar init`` must leave no DETACHED git auto-maintenance behind (bug 3ce7).

At git's defaults a ``git commit`` ends by spawning
``git maintenance run --auto --quiet --detach`` -- a background child that OUTLIVES the
command that spawned it. Against a rebar store that child repacks the object database the
tracker worktree SHARES with its host repo, OUTSIDE rebar's write lock: the store-corruption
hazard ADR 0051 / bug 88eb-2beb-65f5-4bc0 forced into the foreground, and the concurrent
mutator behind the fixture-remote flake family documented in ``tests/_git_upkeep.py``
(bugs dca1-f641-caeb-4df4, b394-6198-6010-42f7, 5b74-5d8f-a6b4-4674, 57d2-e356-7eb4-4bf5).

``_gc_config_unit`` pins ``gc.autoDetach``/``maintenance.autoDetach`` to ``false``, but it
used to run only in the post-bootstrap ensure sweep -- AFTER ``_mount_or_create_branch`` and
``_commit_precommit`` had already committed. Those commits ran at git's defaults and left two
detached children racing every later reader of that object database, which is how
``test_pinned_ticket_view.py``'s ``[packed]`` case lost the loose object it had just written
and failed on its own scaffolding, costing Gerrit change 2682 a spurious ``Verified -1``.

The assertion here is a content fact about an argv list recorded by ``GIT_TRACE2_EVENT``. It
has no timing bound, polls nothing and does not care how long maintenance takes -- only that
none of it is still running once ``rebar init`` has returned.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from _git_upkeep import maintenance_children

import rebar
from rebar import config


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def _recorded_commands(trace: Path) -> list[str]:
    """Every git command name the trace recorded, so a control can prove coverage."""
    names: list[str] = []
    if not trace.exists():
        return names
    for line in trace.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:  # pragma: no cover - trace2 writes well-formed JSON
            continue
        if event.get("event") == "cmd_name":
            names.append(str(event.get("name")))
    return names


@pytest.fixture
def traced_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Run ``rebar init`` on a fresh repository under ``GIT_TRACE2_EVENT``."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    monkeypatch.setenv("REBAR_ROOT", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))
    config.reset_config_cache()
    rebar.init_repo(repo_root=str(root))
    return root, trace


def test_init_spawns_no_detached_maintenance(traced_init: tuple[Path, Path]) -> None:
    """No maintenance child outlives the ``rebar init`` that triggered it."""
    _root, trace = traced_init

    # Positive control: the trace must actually cover the commits made during init, which
    # is where a detached child is spawned. Without this the assertion below would pass
    # just as happily against an empty or unwritten trace file.
    assert "commit" in _recorded_commands(trace), (
        f"trace2 at {trace} recorded no git commit during init, so it cannot witness the "
        f"upkeep those commits spawn. Recorded instead: {sorted(set(_recorded_commands(trace)))}"
    )

    detached = [argv for argv in maintenance_children(trace) if "--detach" in argv]
    assert not detached, (
        "rebar init left detached background git maintenance running against the store's "
        f"object database: {detached}. Pin the auto-maintenance posture (_gc_config_unit) "
        "BEFORE the store's first commit, not in the post-bootstrap ensure sweep."
    )


def test_init_pins_the_no_detach_posture_on_the_store(traced_init: tuple[Path, Path]) -> None:
    """The three settings the pin writes are present once init returns."""
    root, _trace = traced_init
    tracker = str(config.tracker_dir(str(root)))

    for key in ("gc.autoDetach", "maintenance.autoDetach"):
        assert _git(Path(tracker), "config", "--get", key) == "false", (
            f"{key} must be false so a triggered maintenance run stays in the foreground "
            "of the command that holds rebar's write lock (ADR 0051)."
        )
    unset = subprocess.run(
        ["git", "-C", tracker, "config", "--get", "gc.auto"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert unset.returncode != 0, (
        "gc.auto must stay UNSET so auto-gc still bounds loose-object growth at git's "
        f"default threshold; found {unset.stdout.strip()!r}."
    )
