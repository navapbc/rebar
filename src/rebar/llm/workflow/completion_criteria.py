"""The completion verifier's criteria contract: enumeration, admission bounds, coverage.

Three deterministic questions the recovery pipeline must answer identically everywhere:

* **What counts as a criterion?** :func:`explicit_completion_criteria` parses the mechanically
  enumerable completion surface (Markdown checkboxes under canonical ``## Acceptance
  Criteria`` sections, plus a bug's implied resolution criterion) — recovery never asks an
  already-exhausted agent to rediscover scope.
* **How much recovery work is admissible?** :func:`physical_context_ceiling` derives the hard
  per-run context bound from the resolved model's own window, and
  :func:`_validate_recovery_inputs` rejects hostile/unbounded work (criterion count, per- and
  total-criterion characters, context size) before the first recovery call.
* **What does full coverage mean?** :func:`_validate_coverage` fails closed unless a finalized
  verdict covers every expected criterion by ID (order-insensitive, story 2948) with typed
  decisions and no unmet-criterion PASS.

The orchestration that consumes this contract lives in
:mod:`rebar.llm.workflow.completion_recovery`; the bank/pool mechanics in
:mod:`rebar.llm.workflow.completion_banking`.
"""

from __future__ import annotations

import re
from typing import Any

from rebar.llm.errors import CompletionRecoveryError

_CHECKBOX = re.compile(r"(?m)^\s*-\s*\[[ xX]\]\s*(?P<text>\S.*)$")
_AC_HEADING = re.compile(r"^\s*##\s+acceptance criteria\b", re.IGNORECASE)
_H2_HEADING = re.compile(r"^\s*##(?:\s+|$)")
_MAX_CRITERIA = 32
_MAX_CRITERION_CHARS = 4_000
_MAX_TOTAL_CRITERIA_CHARS = 32_000
# PHYSICAL context ceiling. Each verifier run (primary or successor) must fit ONE model
# window. Derived from the resolved verifier model's OWN window (model_classes.own_window_tokens)
# at a deliberately conservative 2 chars/token (English prose averages ~4), leaving the other
# half of the window for the system prompt, criteria, tool traffic, and output. The old ECONOMIC
# product bound (len(context) × len(criteria)) is RETIRED: story 2948 deleted the per-criterion
# fan-out that re-sent the context once per criterion, so spend no longer scales with the
# criterion count — a successor batches ≤ batch_cap criteria over ONE full-context run.
_CONTEXT_CHARS_PER_TOKEN = 2


def explicit_completion_criteria(ticket: dict[str, Any]) -> list[str]:
    """Return stable, explicit checklist requirements from a ticket.

    Recovery deliberately does not ask an already-exhausted agent to rediscover
    scope. Explicit Markdown checkboxes under canonical ``## Acceptance
    Criteria`` sections are the mechanically enumerable completion surface.
    Bugs also get an independent resolution criterion; other ticket types
    without such checkboxes fail closed.
    """

    description = str(ticket.get("description") or "")
    completion_lines: list[str] = []
    in_completion_section = False
    for line in description.splitlines():
        if _AC_HEADING.match(line):
            in_completion_section = True
            continue
        if _H2_HEADING.match(line):
            in_completion_section = False
            continue
        if in_completion_section:
            completion_lines.append(line)

    criteria: list[str] = []
    seen: set[str] = set()
    for match in _CHECKBOX.finditer("\n".join(completion_lines)):
        text = match.group("text").strip()
        if text and text not in seen:
            seen.add(text)
            criteria.append(text)
    title = str(ticket.get("title") or "ticket").strip()
    ticket_type = ticket.get("ticket_type") or ticket.get("type")
    if str(ticket_type or "").strip().lower() == "bug":
        bug_core = (
            f"Bug '{title}' is actually resolved: the reported defect no longer "
            "reproduces and expected behavior holds."
        )
        if bug_core not in seen:
            criteria.append(bug_core)
    if not criteria:
        raise CompletionRecoveryError(
            "cannot enumerate completion recovery criteria for a non-bug ticket "
            "without explicit acceptance-criteria checkboxes"
        )
    return criteria


def physical_context_ceiling(model: str | None) -> int:
    """The max recovery context in chars: the resolved model's own window * 2 chars/token.

    Story a9dd note: the completion gate now pre-loads the ticket's declared file_impact
    contents + referencing-commit diffs into a ``<prefetched_file_contents>`` section that is
    part of the assembled ``context`` string — so those prefetch bytes are ALREADY counted by
    ``_validate_recovery_inputs``'s ceiling check below. ``gate_ops`` pre-trims that section to
    THIS ceiling via ``completion_prefetch.fit_within_ceiling`` before assembling the context,
    so an oversize prefetch is trimmed at the gate rather than tripping this validator here."""
    from rebar.llm.model_classes import own_window_tokens

    return own_window_tokens(model) * _CONTEXT_CHARS_PER_TOKEN


def _validate_recovery_inputs(criteria: list[str], context: str, model: str | None) -> None:
    """Reject hostile/unbounded recovery work before the first recovery call."""

    if len(criteria) > _MAX_CRITERIA:
        raise CompletionRecoveryError(
            "completion recovery criterion-count bound exceeded",
            diagnostic={
                "criteria_total": len(criteria),
                "criteria_limit": _MAX_CRITERIA,
                "criteria_completed": 0,
            },
        )
    oversized = next(
        (
            (index, len(criterion))
            for index, criterion in enumerate(criteria)
            if len(criterion) > _MAX_CRITERION_CHARS
        ),
        None,
    )
    if oversized is not None:
        index, chars = oversized
        raise CompletionRecoveryError(
            "completion recovery per-criterion character bound exceeded",
            diagnostic={
                "criterion_index": index,
                "criterion_chars": chars,
                "criterion_char_limit": _MAX_CRITERION_CHARS,
                "criteria_completed": 0,
            },
        )
    total_chars = sum(len(criterion) for criterion in criteria)
    if total_chars > _MAX_TOTAL_CRITERIA_CHARS:
        raise CompletionRecoveryError(
            "completion recovery total criterion character bound exceeded",
            diagnostic={
                "criteria_chars": total_chars,
                "criteria_char_limit": _MAX_TOTAL_CRITERIA_CHARS,
                "criteria_completed": 0,
            },
        )
    context_ceiling = physical_context_ceiling(model)
    if len(context) > context_ceiling:
        raise CompletionRecoveryError(
            "completion recovery context bound exceeded",
            diagnostic={
                "context_chars": len(context),
                "context_char_limit": context_ceiling,
                "criteria_completed": 0,
            },
        )


def _validate_coverage(
    result: dict[str, Any], expected: list[str], id_by_text: dict[str, str]
) -> None:
    """Fail closed unless the verdict covers every expected criterion — by criterion ID,
    ORDER-INSENSITIVE (story 2948).

    The old contract required an ordered full-list equality and so could not accept a
    remainder-scoped or banked-union result; the reworked contract accepts the banked-union-
    successor set as long as the returned criterion IDs, as a SET, cover the expected IDs.
    Retry-on-missing is preserved: a record short of full coverage raises
    ``CompletionRecoveryError`` naming the count so the caller can retry/backfill.
    """
    records = result.get("criteria")
    if not isinstance(records, list):
        raise CompletionRecoveryError(
            "completion recovery finalizer omitted per-criterion coverage",
            diagnostic={"criteria_total": len(expected), "criteria_returned": 0},
        )
    returned_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        cid = record.get("criterion_id")
        if not cid:
            cid = id_by_text.get(str(record.get("criterion") or "").strip())
        if cid:
            returned_ids.add(str(cid))
    expected_ids = {id_by_text[text] for text in expected}
    if not expected_ids.issubset(returned_ids):
        raise CompletionRecoveryError(
            "completion recovery finalizer returned incomplete criterion coverage",
            diagnostic={
                "criteria_total": len(expected),
                "criteria_returned": len(returned_ids),
                "coverage_exact": False,
            },
        )
    if any(not isinstance(record.get("met"), bool) for record in records):
        raise CompletionRecoveryError(
            "completion recovery finalizer returned an untyped criterion decision",
            diagnostic={"criteria_total": len(expected), "coverage_exact": True},
        )
    verdict = str(result.get("verdict") or "").strip().upper()
    if verdict == "PASS" and any(not record["met"] for record in records):
        raise CompletionRecoveryError(
            "completion recovery finalizer returned an unmet criterion with a PASS verdict",
            diagnostic={
                "criteria_total": len(expected),
                "criteria_unmet": sum(not record["met"] for record in records),
                "coverage_exact": True,
            },
        )
