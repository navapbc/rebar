"""Ticket 3803 — make ``transition --reason`` honest.

Before this change ``rebar transition --help`` advertised ``[--reason=<text>]`` on every
transition, but on a plain (non-``--force``) transition the value was parsed, threaded
through two call layers, and then DISCARDED (``transition_core``'s ``close_reason`` param
was never read). A user closing a ticket with ``--reason="…"`` reasonably believed the
rationale was recorded — it was not.

The fix makes the flag honest without adding persistence (ed13 deliberately replaced
free-text close rationale with the bounded ``--class`` vocabulary — see the ticket):

- AC1 (as amended by ticket fc20): ``transition_core`` originally LOST the discarded
  ``close_reason`` slot outright. fc20's administrative dispositions deliberately
  re-added it — but the honesty rule this ticket established still holds: the parameter
  is keyword-only with an empty default, and the value PERSISTS only on a close whose
  class requires a reason (``obsolete``/``wontfix``); every other call discards nothing
  silently because nothing is accepted-and-dropped — the guards below pin that.
- AC3: passing ``--reason`` on a plain (non-force) transition is REFUSED, not silently
  accepted and dropped — except (fc20) on a close whose ``--class`` is obsolete/wontfix,
  where the reason is REQUIRED and recorded as ``close_reason``.
- AC5's historical flag-coexistence remains: ``--reason`` is not rejected merely because
  ``--force`` is present. Ticket blusterous-earthly-kitten later removed its audit-note
  fallback role; force audit text now rides only on ``--force=<reason>``.

``open -> blocked`` is used as the neutral probe: it is neither a start-work
(``in_progress``, plan-review gate) nor a close (completion gate) edge, so it isolates the
flag semantics from the gates.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import rebar
from rebar import _cli


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


# ── AC3: --reason on a plain (non-force) transition is refused, not dropped ──────────


def test_reason_on_plain_transition_is_refused(rebar_repo: Path, capsys) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "open"

    rc = _cli.main(["transition", tid, "open", "blocked", "--reason=please record this"])

    # Refused (non-zero), and — crucially — NOT silently accepted-and-dropped: the
    # ticket did not move.
    assert rc != 0
    assert _status(tid, rebar_repo) == "open"
    err = capsys.readouterr().err
    assert "--reason" in err
    assert "force" in err.lower()


# ── Held-out oracle (edge / contract) ───────────────────────────────────────────────


def test_reason_allowed_under_force(rebar_repo: Path) -> None:
    """AC5 compatibility: the flags may coexist, although reason is no longer a force note."""
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))

    rc = _cli.main(["transition", tid, "open", "blocked", "--force", "--reason=hatch"])

    assert rc == 0
    assert _status(tid, rebar_repo) == "blocked"


def test_plain_transition_without_reason_unaffected(rebar_repo: Path) -> None:
    """Negative control: a plain transition with no ``--reason`` is unchanged."""
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))

    rc = _cli.main(["transition", tid, "open", "blocked"])

    assert rc == 0
    assert _status(tid, rebar_repo) == "blocked"


def test_transition_core_close_reason_contract() -> None:
    """AC1, amended by ticket fc20: the slot exists again for administrative dispositions,
    but on the honest terms this ticket established — keyword-only, empty default, so no
    positional caller can smuggle a value in by accident."""
    from rebar._commands import txn

    param = inspect.signature(txn.transition_core).parameters["close_reason"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default == ""


def _close_via_core(tid: str, repo: Path, **kwargs) -> dict:
    from rebar import config
    from rebar._commands import txn
    from rebar._engine_support.resolver import resolve_ticket_id

    rebar.claim(tid, assignee="me", repo_root=str(repo))
    tracker = str(config.tracker_dir(str(repo)))
    resolved = resolve_ticket_id(tid, tracker)
    assert resolved is not None
    txn.transition_core(
        tracker,
        resolved,
        "in_progress",
        "closed",
        env_id="test-env",
        author="test",
        repo_root=str(repo),
        **kwargs,
    )
    return rebar.show_ticket(resolved, repo_root=str(repo))


def test_close_without_a_reason_persists_no_close_reason(rebar_repo: Path) -> None:
    """The pre-fc20 shape: an ordinary close writes NO close_reason key (present-only),
    so its STATUS event stays byte-identical to the pre-feature event."""
    tid = rebar.create_ticket("task", "plain close", repo_root=str(rebar_repo))

    state = _close_via_core(tid, rebar_repo)

    assert state["status"] == "closed"
    assert "close_reason" not in state


def test_close_reason_with_a_non_reason_class_is_not_persisted(rebar_repo: Path) -> None:
    """The 3803 honesty rule, enforced write-side: a caller passing ``close_reason``
    alongside a class that does not take one gets the value DISCARDED at the write, not
    smuggled past the bounded ``--class`` vocabulary."""
    tid = rebar.create_ticket("bug", "smuggling probe", repo_root=str(rebar_repo))

    state = _close_via_core(
        tid, rebar_repo, close_class="regression", close_reason="smuggled rationale"
    )

    assert state["close_class"] == "regression"
    assert "close_reason" not in state


def test_close_reason_with_a_reason_required_class_is_persisted(rebar_repo: Path) -> None:
    """Positive control (ticket fc20): the ONE admitted shape — a reason-required
    administrative class — records the justification as ``close_reason``."""
    tid = rebar.create_ticket("task", "obsolete premise", repo_root=str(rebar_repo))

    state = _close_via_core(
        tid, rebar_repo, close_class="obsolete", close_reason="premise no longer holds"
    )

    assert state["close_class"] == "obsolete"
    assert state["close_reason"] == "premise no longer holds"
