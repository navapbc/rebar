"""False-positive ledger for the code-review gate (story 1669).

A confirmed false-positive / invalid code-review finding is recorded as a
``bug``-typed rebar ticket tagged ``fp:code-review`` (the tag convention +
required body fields are documented in ``docs/code-review-fp-ledger.md``).
:func:`compile_fp_ledger` turns each such OPEN ticket into a NO-FIRE eval case
(the ``expect: pass`` shape the ``code-review-*.eval.yaml`` datasets already use),
so the incident becomes a standing regression the overlay is held to.

This is ADVISORY tooling only — it never runs inside the gate and never touches a
verdict. It is manual to start (no scheduling): call :func:`compile_fp_ledger` and
feed the drafted cases into an eval dataset. It is IDEMPOTENT — a compiled ticket
is tagged ``compiled`` so a re-run skips it — and error-ISOLATED — one malformed
ticket is skipped (not fatal), and a store-read failure surfaces as an empty list.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────────────
#: A diff is "non-trivial" (grounding worth checking) above this many changed lines.
NON_TRIVIAL_DIFF_LINES = 20
#: Rule-of-Three: this many surviving high-priority findings hints the approach itself
#: (not a nit) may be under review — a viability signal, not a block.
MIN_SURVIVING_HIGH_PRIORITY = 3
#: A Pass-2 drop-rate at/above this fraction hints the overlay may be over-firing.
MAX_PASS2_DROP_RATE = 0.5

#: The closed root-cause enum a ledger ticket must classify its false positive under.
FP_ROOT_CAUSES: frozenset[str] = frozenset(
    {
        "false-evidence",
        "rubric-overapplication",
        "hallucinated-gap",
        "scope-mismatch",
        "stale-baseline",
    }
)

#: The tag that marks a ticket as an FP-ledger entry, and the tag stamped once compiled.
FP_TAG = "fp:code-review"
COMPILED_TAG = "compiled"
#: The eval-dataset corpus the drafted no-fire cases belong to.
CORPUS = "fp-ledger"

# A ``root-cause: <value>`` (or ``root cause:`` / ``rootcause:``) line, anywhere in the body.
_ROOT_CAUSE_RE = re.compile(r"^\s*root[\s-]?cause\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
# The first fenced code block — the diff/context that triggered the finding. The optional
# ``diff`` info-string is stripped; the inner body is returned verbatim.
_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_-]*)\n(.*?)```", re.DOTALL)


def is_non_trivial_diff(changed_files: int, changed_lines: int) -> bool:
    """Whether a change is big enough that under-grounding is a real risk: more than
    :data:`NON_TRIVIAL_DIFF_LINES` changed lines, OR touching more than one file."""
    return changed_lines > NON_TRIVIAL_DIFF_LINES or changed_files > 1


def _tid(ticket: Mapping[str, Any]) -> str:
    """The ticket's canonical id (``list_tickets`` returns it under ``ticket_id``)."""
    return str(ticket.get("ticket_id") or ticket.get("id") or "")


def _slug(ticket: Mapping[str, Any]) -> str:
    """A stable case id from the ticket's alias (preferred) or canonical id."""
    ident = str(ticket.get("alias") or "").strip() or _tid(ticket)
    return f"FP-{ident}" if ident else "FP-unknown"


def _parse_root_cause(body: str) -> str | None:
    """The ``root-cause:`` value from the body, lower-cased, iff it is one of the closed
    :data:`FP_ROOT_CAUSES`; else None (a missing/unknown value ⇒ the ticket is skipped)."""
    m = _ROOT_CAUSE_RE.search(body)
    if not m:
        return None
    value = m.group(1).strip().lower()
    return value if value in FP_ROOT_CAUSES else None


def _parse_diff(body: str) -> str | None:
    """The diff/context that triggered the finding — the first fenced code block's body.
    None when the ticket carries no fenced block (⇒ the ticket is skipped)."""
    m = _FENCE_RE.search(body)
    if not m:
        return None
    diff = m.group(1).strip("\n")
    return diff or None


def _draft_case(ticket: Mapping[str, Any]) -> dict[str, Any] | None:
    """DRAFT a no-fire eval case from an FP-ledger ticket, in the eval-spec dataset shape
    (``{id, corpus, expect, mode, diff}``). Returns None when a REQUIRED field (root-cause
    or diff/context) is missing/malformed, so the caller skips the ticket."""
    body = str(ticket.get("description") or "")
    root_cause = _parse_root_cause(body)
    if root_cause is None:
        return None
    diff = _parse_diff(body)
    if diff is None:
        return None
    return {
        "id": _slug(ticket),
        "corpus": CORPUS,
        "expect": "pass",
        "mode": root_cause,
        "diff": diff,
    }


def compile_fp_ledger(repo_root: str | None = None) -> list[dict]:
    """Draft a no-fire eval case for every OPEN ``fp:code-review`` ticket not yet ``compiled``.

    Reads the ledger tickets via the rebar library, drafts a case per uncompiled ticket, and
    (idempotently) stamps :data:`COMPILED_TAG` on each drafted ticket so a re-run skips it.
    Fully error-isolated: a store-read failure returns ``[]`` (never raises into the caller),
    and one malformed/unreadable ticket is logged-and-skipped without aborting the batch."""
    import rebar

    try:
        tickets = rebar.list_tickets(status="open", has_tag=FP_TAG, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — a store/read error yields no cases, never raises
        logger.warning("fp-ledger: could not read %r tickets; returning no cases", FP_TAG)
        return []

    cases: list[dict] = []
    for ticket in tickets:
        tid = _tid(ticket)
        try:
            if COMPILED_TAG in (ticket.get("tags") or []):
                continue  # already compiled — idempotent skip
            case = _draft_case(ticket)
            if case is None:
                logger.info(
                    "fp-ledger: skipping malformed ticket %s (missing root-cause or diff)", tid
                )
                continue
            # Mark compiled BEFORE recording the case so a tag failure leaves the ticket
            # untagged AND the case undrafted (a clean re-run), never a duplicate.
            rebar.tag(tid, COMPILED_TAG, repo_root=repo_root)
            cases.append(case)
        except Exception:  # noqa: BLE001 — one bad ticket never aborts the batch
            logger.warning("fp-ledger: skipping ticket %s after error", tid)
            continue
    return cases


__all__ = [
    "NON_TRIVIAL_DIFF_LINES",
    "MIN_SURVIVING_HIGH_PRIORITY",
    "MAX_PASS2_DROP_RATE",
    "FP_ROOT_CAUSES",
    "is_non_trivial_diff",
    "compile_fp_ledger",
]
