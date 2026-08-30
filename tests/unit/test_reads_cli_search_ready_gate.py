"""CLI ``search`` / ``ready`` must reject a present-but-UNUSABLE (and absent) store.

WHY THIS TEST EXISTS. ``rapt-dreadable-dromedary`` (aefe-614a-2631-4117) hardened the
in-process read chokepoint (``rebar._reads._tracker``) and the write gate to the content-aware
``rebar._store.store_usability.store_is_usable`` predicate, and ``be80-8377-4b4c-44f4``
(change 2412) extended that gate to the CLI ``list`` / ``session-logs`` arms.

Two CLI read arms were left with **no readiness gate at all**: ``_cmd_search`` and ``_cmd_ready``
in ``rebar._engine_support/reads_cli.py`` call ``search_state`` / ``ready_states`` DIRECTLY,
bypassing both the bare-``isdir`` check the sibling arms carried and the hardened
``_reads._tracker`` chokepoint. So on the CLI a broken/mid-clone store — AND an absent store —
printed ``[]`` at exit 0, the exact fallback-masking rapt-d removed from the library, surviving
on the CLI surface (an agent is told "no ready work" when the truth is "store is broken";
97e9-e663-a94e-4038). These tests pin the strict contract (the owner's AC-1 decision): both arms
reject a present-but-unusable store AND an absent store (exit 1, "not initialized"), while a
valid, EMPTY, initialized store still returns ``[]`` at exit 0.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._engine_support import reads_cli

_NOT_INITIALIZED = "not initialized"


def _tracker_present_without_git(tmp_path: Path) -> Path:
    """A tracker DIRECTORY that exists (holds a marker file) but has NO ``.git`` — ``isdir``
    is true, so a bare existence check would pass it through and the read returned ``[]``.
    Nested inside a git-initialised code repo so the predicate's ordering matters (a naive
    ``git -C tracker rev-parse HEAD`` would walk UP to the enclosing repo)."""
    repo = tmp_path / "brokenrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    tracker = repo / ".tickets-tracker"
    tracker.mkdir()
    (tracker / "reviewbot-ensure-tickets").write_text("marker\n")
    assert tracker.is_dir() and not (tracker / ".git").exists()
    return tracker


def _tracker_midclone_unresolvable_head(tmp_path: Path) -> Path:
    """A store MID-CLONE: the tracker ``.git`` is present but HEAD does not resolve yet (an
    unborn branch). ``isdir`` and ``.git``-presence are both true, so only a HEAD-resolvability
    probe distinguishes it from a finished clone."""
    repo = tmp_path / "midclone"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    tracker = repo / ".tickets-tracker"
    tracker.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tracker, check=True)
    probe = subprocess.run(
        ["git", "-C", str(tracker), "rev-parse", "--verify", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode != 0, "fixture must present an UNRESOLVABLE HEAD"
    assert (tracker / ".git").exists()
    return tracker


def _initialized_empty_tracker(tmp_path: Path) -> Path:
    """An initialized store holding zero tickets — the legitimate ``[]`` case (exit 0)."""
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    rebar.init_repo(repo_root=repo)
    tracker = repo / ".tickets-tracker"
    assert tracker.is_dir()
    return tracker


def _absent_tracker(tmp_path: Path) -> Path:
    """A tracker path that does not exist at all — the ABSENT case."""
    return tmp_path / "nope" / ".tickets-tracker"


# The two CLI read arms that carried NO readiness gate, driven directly through their
# argv-facing handler (the tracker is an explicit parameter for exactly this). The argv is
# chosen so a valid-empty store renders ``[]`` for a uniform assertion: ``search`` takes a
# query positional (json by default); ``ready`` is forced to ``--output json`` (its text
# default emits one id per line, i.e. empty output on an empty store).
_CLI_ARMS = [
    ("search", reads_cli._cmd_search, ["zzz"]),
    ("ready", reads_cli._cmd_ready, ["--output", "json"]),
]


def _drive(handler, argv: list[str], tracker: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = handler(argv, str(tracker))
    return rc, out.getvalue(), err.getvalue()


@pytest.mark.parametrize(("name", "handler", "argv"), _CLI_ARMS)
def test_cli_search_ready_reject_present_but_unusable_store_without_git(
    tmp_path: Path, name: str, handler, argv: list[str]
) -> None:
    """A tracker dir that exists but holds no ``.git`` is a BROKEN store: exit 1, not ``[]``."""
    tracker = _tracker_present_without_git(tmp_path)

    rc, out, err = _drive(handler, argv, tracker)

    assert rc == 1, f"{name} must reject a `.git`-less tracker (exit 1), got {rc} with out={out!r}"
    assert _NOT_INITIALIZED in err, f"{name} must name the uninitialized store, got: {err!r}"


@pytest.mark.parametrize(("name", "handler", "argv"), _CLI_ARMS)
def test_cli_search_ready_reject_midclone_store_unresolvable_head(
    tmp_path: Path, name: str, handler, argv: list[str]
) -> None:
    """A store mid-clone (``.git`` present, HEAD unresolvable) is rejected: exit 1, not ``[]``."""
    tracker = _tracker_midclone_unresolvable_head(tmp_path)

    rc, out, err = _drive(handler, argv, tracker)

    assert rc == 1, f"{name} must reject a mid-clone tracker (exit 1), got {rc} with out={out!r}"
    assert _NOT_INITIALIZED in err, f"{name} must name the uninitialized store, got: {err!r}"


@pytest.mark.parametrize(("name", "handler", "argv"), _CLI_ARMS)
def test_cli_search_ready_reject_absent_store(
    tmp_path: Path, name: str, handler, argv: list[str]
) -> None:
    """The strict-contract flip: an ABSENT store now rejects with exit 1 (was ``[]`` rc 0)."""
    tracker = _absent_tracker(tmp_path)

    rc, out, err = _drive(handler, argv, tracker)

    assert rc == 1, f"{name} must reject an absent store (exit 1), got {rc} with out={out!r}"
    assert _NOT_INITIALIZED in err, f"{name} must name the uninitialized store, got: {err!r}"


@pytest.mark.parametrize(("name", "handler", "argv"), _CLI_ARMS)
def test_cli_search_ready_still_return_empty_for_initialized_empty_store(
    tmp_path: Path, name: str, handler, argv: list[str]
) -> None:
    """No-regression: a valid, EMPTY, initialized store still returns ``[]`` at exit 0. This
    keeps the guard honest — it must key on store USABILITY, not on emptiness, so a
    freshly-initialized store cannot be mistaken for a broken one."""
    tracker = _initialized_empty_tracker(tmp_path)

    rc, out, err = _drive(handler, argv, tracker)

    assert rc == 0, f"{name} must accept a valid empty store (exit 0), got {rc} with err={err!r}"
    assert out.strip() == "[]", f"{name} must render an empty store as `[]`, got: {out!r}"


def test_cli_search_ready_gate_on_store_is_usable() -> None:
    """Structural class guard: both CLI read arms must decide readiness with
    ``store_is_usable(tracker)`` — the authority predicate ``rebar._reads._tracker`` uses. This
    fails if the fix is reverted OR if either arm drops the gate (or a bare ``os.path.isdir``
    weak existence check sneaks back in)."""
    import inspect

    for arm in (reads_cli._cmd_search, reads_cli._cmd_ready):
        source = inspect.getsource(arm)
        assert "store_is_usable(tracker)" in source, (
            f"{arm.__name__} must gate readiness on store_is_usable(tracker) for parity with "
            "rebar._reads._tracker (97e9-e663; be80-8377)"
        )
        assert "os.path.isdir(tracker)" not in source, (
            f"{arm.__name__} gates readiness on bare os.path.isdir(tracker) — use "
            "store_is_usable(tracker) instead (97e9-e663)"
        )
