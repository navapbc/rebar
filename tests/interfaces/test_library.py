"""Library-interface tests (rebar package, in-process).

Covers behaviors specific to the Python library surface: typed exceptions, the
native read re-exports, and the fsck/fsck-recover write path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar


def test_fsck_runs(rebar_repo: Path) -> None:
    """fsck() (no recovery) returns the engine's check output."""
    out = rebar.fsck(repo_root=str(rebar_repo))
    assert "fsck" in out.lower()


def test_fsck_recover(rebar_repo: Path) -> None:
    """fsck(recover=True) must run the recovery path, not fail with an unknown
    subcommand.

    Regression: the library maps recover=True -> the 'fsck-recover' subcommand,
    but the dispatcher had no such arm, so it raised
    RebarError("unknown subcommand 'fsck-recover'").
    """
    out = rebar.fsck(recover=True, repo_root=str(rebar_repo))
    assert isinstance(out, str)


def test_concurrency_error_typed(rebar_repo: Path) -> None:
    """A transition with a valid-but-stale current_status raises ConcurrencyError
    (engine exit 10), not a generic RebarError."""
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    with pytest.raises(rebar.ConcurrencyError) as exc:
        # Ticket is 'open'; claim a valid-but-wrong current status.
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert exc.value.returncode == 10


def test_native_reexports_importable() -> None:
    """The stdlib-only native read API is re-exported on the package."""
    for name in (
        "reduce_all_tickets",
        "reduce_ticket",
        "to_llm",
        "find_inbound_relationships",
        "apply_ticket_filters",
    ):
        assert callable(getattr(rebar, name)), name
