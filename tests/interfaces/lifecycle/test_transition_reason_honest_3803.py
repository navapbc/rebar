"""Ticket 3803 — make ``transition --reason`` honest.

Before this change ``rebar transition --help`` advertised ``[--reason=<text>]`` on every
transition, but on a plain (non-``--force``) transition the value was parsed, threaded
through two call layers, and then DISCARDED (``transition_core``'s ``close_reason`` param
was never read). A user closing a ticket with ``--reason="…"`` reasonably believed the
rationale was recorded — it was not.

The fix makes the flag honest without adding persistence (ed13 deliberately replaced
free-text close rationale with the bounded ``--class`` vocabulary — see the ticket):

- AC1: ``close_reason`` is removed from ``transition_core``'s signature.
- AC3: passing ``--reason`` on a plain (non-force) transition is REFUSED, not silently
  accepted and dropped.
- AC5: ``--force`` / ``--force=<reason>`` behaviour is unchanged — ``--reason`` still
  serves as the audit-note fallback under ``--force``.

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
    """AC5: under ``--force`` the ``--reason`` text is the audit-note fallback, so it must
    NOT be refused — the transition proceeds."""
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


def test_transition_core_has_no_close_reason_param() -> None:
    """AC1: the discarded ``close_reason`` slot is gone from the signature, so no caller
    can pass a value that the body never reads."""
    from rebar._commands import txn

    params = inspect.signature(txn.transition_core).parameters
    assert "close_reason" not in params
