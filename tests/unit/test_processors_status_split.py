"""Oracle for the STATUS-cluster split (ticket ce02).

``process_status`` and its three exclusive folds moved from
``rebar.reducer._processors`` into ``rebar.reducer._processors_status``. The move is
behaviour-preserving, so the oracle proves (a) every moved name still resolves through the
old paths its consumers use, (b) the split is one-way (no import back into ``_processors``),
and (c) replaying a status sequence still folds status, plan-review phase, claimed session
and close metadata identically.
"""

from __future__ import annotations

import ast
from pathlib import Path

import rebar.reducer as reducer_pkg
import rebar.reducer._processors as processors
import rebar.reducer._processors_status as processors_status

_MOVED = (
    "_fold_plan_review_phase",
    "_fold_claimed_session",
    "_fold_close_metadata",
    "process_status",
)


def test_moved_names_resolve_from_processors_and_are_object_identical() -> None:
    """Every moved name still resolves from ``_processors`` and is the SAME object as in
    ``_processors_status`` — this is the form ``_replay`` and ``reducer.__init__`` import."""
    for name in _MOVED:
        assert hasattr(processors, name), f"{name} no longer resolves from _processors"
        assert getattr(processors, name) is getattr(processors_status, name)


def test_process_status_resolves_from_reducer_package() -> None:
    """``rebar.reducer.process_status`` (the public re-export) survives the move."""
    assert reducer_pkg.process_status is processors_status.process_status


def test_split_is_one_way_no_import_of_processors() -> None:
    """``_processors_status`` must not import ``_processors`` (else the split is not one-way)."""
    source = Path(processors_status.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert mod not in {"_processors", "rebar.reducer._processors"}, (
                f"one-way violation: _processors_status imports from {mod}"
            )
            assert not (node.level and mod == "_processors")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "._processors" not in alias.name or "_status" in alias.name


def _apply_status(state: dict, event_uuid: str, current: str | None, target: str, **data):
    event = {"uuid": event_uuid, "env_id": "e", "timestamp": 1}
    payload = {"current_status": current, "status": target, **data}
    processors_status.process_status(state, event, payload, "")
    return state


def test_status_fold_replay_equivalence() -> None:
    """A crafted STATUS replay still folds status, plan_review_phase, claimed_session and
    close_class — the assertion that proves a fold did not silently drop in the move."""
    state: dict = {"status": "open", "ticket_id": "t1"}

    # open -> in_progress: status, execution phase, claimed session + harness all fold.
    _apply_status(
        state, "u1", "open", "in_progress", session="s-42", harness="cli", remote_session="r"
    )
    assert state["status"] == "in_progress"
    assert state["plan_review_phase"] == "execution"
    assert state["claimed_session"] == "s-42"
    assert state["claim_harness"] == "cli"
    assert state["claim_remote_session"] == "r"

    # in_progress -> closed with a bug class + force reason: close metadata folds.
    _apply_status(
        state, "u2", "in_progress", "closed", close_class="wontfix", force_close_reason="ops"
    )
    assert state["status"] == "closed"
    assert state["close_class"] == "wontfix"
    assert state["force_close_reason"] == "ops"

    # closed -> open reopen: phase returns to planning and reopen timestamp records.
    _apply_status(state, "u3", "closed", "open")
    assert state["status"] == "open"
    assert state["plan_review_phase"] == "planning"
    assert state["last_reopened_at"] == 1


def test_both_modules_under_the_size_target() -> None:
    """AC: each file at or below 650 lines (asserted explicitly, not via the 800 gate — E6)."""
    for mod in (processors, processors_status):
        n = len(Path(mod.__file__).read_text().splitlines())
        assert n <= 650, f"{Path(mod.__file__).name} is {n} lines (> 650)"
