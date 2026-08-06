"""The context-bound remedy must name a lever the operator can actually pull (bug db1c).

`d59e` raised the bounded-recovery context cap from 24,000 to 100,000 characters, which
removed the *refusal* for every real ticket observed. It did not touch the sentence the
refusal prints, so the remaining defect is the remedy itself:

    The ticket's context (100,001 chars) exceeds the bounded completion-recovery limit
    (100,000 chars), so the recovery pass was refused before it ran — shorten the
    ticket's description/comments.

Both halves of "shorten the ticket's description/comments" are unactionable, for two
*different* reasons, and each is contradicted by code that is already on `main`:

* **description** — `rebar.llm.plan_review.pass1.material_fingerprint` hashes the
  description into the plan-review attestation ("a material edit invalidates the
  signature"). Editing it to satisfy the completion gate therefore breaks the *other*
  close-gate precondition, `require_plan_review_for_close`, which then demands the
  attestation be re-earned. The advertised remedy for one close gate is a guaranteed
  failure of the other, and the message says nothing about that trade.
* **comments** — comments are excluded from the fingerprint, so editing them would be
  safe, but the store is append-only: `rebar._lib_writes` exposes `comment()` with no
  delete or redact counterpart. There is no operation that shortens a comment.

`d59e` shipped a test whose own docstring records this — "the remedy it printed
('shorten the ticket's description/comments') could not be carried out" (see
`tests/unit/workflow/test_completion_recovery_oversized_d59e.py`) — while leaving the
message that prints it unchanged. That contradiction between landed code and landed
message is what this file closes.

What this pins:

* the message never advises editing the description without naming the attestation cost;
* the message never offers comment-shortening, which the append-only store cannot do;
* the message still reports the measured size and the limit, so removing the false
  remedy does not cost the operator the diagnosis (the guarantee
  `test_close_gate_bound_message_d59e.py` established, re-asserted here so a rewrite of
  the sentence cannot silently drop it).
"""

from __future__ import annotations

import re

import pytest

from rebar._commands import gates as _gates
from rebar._commands import transition_close as _tc
from rebar._commands._seam import CommandError
from rebar._engine_support import field_reads as _fr
from rebar.llm.config import VERIFIER_DEFAULT_MODEL
from rebar.llm.errors import CompletionRecoveryError

pytestmark = pytest.mark.unit

# The real ticket db1c was filed against (2932), at the size that produced the live
# failure. The cap is now larger than this, so the fixture deliberately breaches the
# CURRENT cap rather than hard-coding a number that a future raise would make stale.
_TICKET_2932_CHARS = 41_595


def _context_ceiling() -> int:
    """The physical context ceiling for the default verifier model (bug 8eb3 retired the
    flat `_MAX_CONTEXT_CHARS`; the bound is now window-derived)."""
    from rebar.llm.workflow import completion_recovery as _cr

    return _cr.physical_context_ceiling(VERIFIER_DEFAULT_MODEL)


def _bound_error() -> CompletionRecoveryError:
    """The exact exception `_validate_recovery_inputs` raises on a context breach."""
    limit = _context_ceiling()
    return CompletionRecoveryError(
        "completion recovery context bound exceeded",
        diagnostic={
            "context_chars": limit + 1,
            "context_char_limit": limit,
            "criteria_completed": 0,
        },
    )


def _arm_gate(monkeypatch, exc: Exception) -> None:
    """Enable the close gate and make `verify_completion` raise `exc`."""
    import rebar.llm as _llm

    monkeypatch.setattr(_gates, "gate_enabled", lambda *a, **k: True)
    monkeypatch.setattr(_fr, "file_impact", lambda *a, **k: [])

    def _raise(*_a, **_k):
        raise exc

    monkeypatch.setattr(_llm, "verify_completion", _raise)


def _close_message(monkeypatch, exc: Exception) -> str:
    """Drive the operator-visible close path and return the CLI's error text."""
    _arm_gate(monkeypatch, exc)
    with pytest.raises(CommandError) as caught:
        _tc._completion_precheck("rec-0000", "task", ".", None, reason="", force_close="")
    return str(caught.value)


# "shorten/trim/reduce/edit ... description" with no attestation caveat anywhere.
_DESCRIPTION_ADVICE = re.compile(
    r"(shorten|trim|reduce|shrink|edit|cut)[^.]{0,80}\bdescription\b", re.IGNORECASE
)
_ATTESTATION_CAVEAT = re.compile(r"attestation|plan[- ]review|re-?earn|signature", re.IGNORECASE)
_COMMENT_ADVICE = re.compile(
    r"(shorten|trim|reduce|shrink|delete|remove|prune)[^.]{0,80}\bcomments?\b", re.IGNORECASE
)


def test_advising_a_description_edit_must_name_the_attestation_cost(monkeypatch) -> None:
    """THE BUG (half 1): the remedy sends the operator to break the plan-review gate.

    `material_fingerprint` binds the description into the signed attestation, so a
    description edit invalidates it and `require_plan_review_for_close` then blocks the
    same close. Advising the edit while silent on that cost is the defect; the message
    may still mention the description, but only with the consequence attached.
    """
    message = _close_message(monkeypatch, _bound_error())

    if _DESCRIPTION_ADVICE.search(message):
        assert _ATTESTATION_CAVEAT.search(message), (
            "the message advises editing the ticket description but never says that doing "
            "so invalidates the plan-review attestation (material_fingerprint hashes the "
            "description), so following it breaks require_plan_review_for_close and the "
            f"close still fails. Got: {message}"
        )


def test_the_remedy_never_offers_comment_shortening(monkeypatch) -> None:
    """THE BUG (half 2): the store is append-only, so this lever does not exist.

    `rebar._lib_writes` exposes `comment()` with no delete/redact counterpart. Offering
    it is not merely unhelpful — it is the one instruction an operator cannot carry out
    at all, which is what made the original bound feel like an inescapable block.
    """
    message = _close_message(monkeypatch, _bound_error())

    assert not _COMMENT_ADVICE.search(message), (
        "the message tells the operator to shorten/delete comments, but the event store is "
        "append-only: _lib_writes exposes comment() with no delete or redact counterpart, "
        f"so no such operation exists. Got: {message}"
    )


def test_the_size_diagnosis_survives_the_new_remedy(monkeypatch) -> None:
    """NEGATIVE CONTROL — removing a false remedy must not remove the true diagnosis.

    Without this, "stop advising a description edit" could be satisfied by emitting a
    bare failure. `test_close_gate_bound_message_d59e.py` established that the bound case
    reports the measured size and the limit; re-asserted here so rewriting the sentence
    cannot silently regress it.
    """
    message = _close_message(monkeypatch, _bound_error())

    limit = _context_ceiling()
    assert f"{limit + 1:,}" in message, (
        f"the measured context size must survive the rewrite. Got: {message}"
    )
    assert f"{limit:,}" in message, f"the limit must survive the rewrite. Got: {message}"


def test_the_remedy_points_at_a_lever_the_operator_can_actually_pull(monkeypatch) -> None:
    """Removing both false levers must leave something actionable behind.

    A message that only says "this is too big" and names no next step trades a wrong
    remedy for no remedy. The sidecar diagnostic is the surface `docs/llm-framework.md`
    already directs the operator to, and `--force-close` is the sanctioned operator
    judgement call; at least one real next step must be named.
    """
    message = _close_message(monkeypatch, _bound_error())

    assert re.search(r"gate_error_v1|sidecar|force-close|diagnostic", message, re.IGNORECASE), (
        "the operator is told the close failed on size but given no next step at all. "
        f"Got: {message}"
    )


def test_a_real_ticket_sized_like_2932_is_no_longer_refused() -> None:
    """Guards the half of db1c that `d59e` already fixed, so it cannot silently regress.

    db1c was filed because ticket 2932 (41,595 chars) was refused against a 24,000-char
    cap. That cap is now 100,000. If a future change lowers it back under this size the
    original defect returns, and the remedy wording above becomes load-bearing again.
    """
    from rebar.llm.workflow import completion_recovery as _cr  # noqa: F401

    ceiling = _context_ceiling()
    assert _TICKET_2932_CHARS <= ceiling, (
        f"ticket 2932 ({_TICKET_2932_CHARS:,} chars) would be refused again by a context "
        f"budget of {ceiling:,}; db1c's original failure has regressed"
    )
