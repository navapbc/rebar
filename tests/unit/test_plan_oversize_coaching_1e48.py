"""Ticket 1e48-ff69-13f7-4a83 — the P4 over-limit message must coach CONCISION.

The over-limit branch used to offer exactly one remedy ("split independent work
into coherent child tickets"). Splitting is right when the **work** is oversized;
it is wrong when the **prose** is, which is the more common cause — and an author
who follows it literally fragments a coherent unit of work for a formatting
reason. These tests pin the *observable message content* of both P4 branches:

* the **over-limit** branch coaches concision + relocating narration to comments
  or a session log, distinguishes context-that-stays from narration-that-moves,
  and points at the packaged authoring guide instead of restating it;
* the **oversize-signal** branch keeps decomposition advice, where it fits;
* the two are distinguishable, and the 8,000-character limit is unchanged.
"""

from __future__ import annotations

from rebar.llm.plan_review.det_floor import (
    P4_AC_SOFT_CAP,
    PlanContext,
    p4_oversize,
)

_AC_BLOCK = "## Acceptance Criteria\n- [ ] the thing is done\n"


def _ctx(description: str) -> PlanContext:
    return PlanContext(
        ticket_id="1e48-ff69-13f7-4a83",
        ticket_type="bug",
        title="t",
        description=description,
    )


def _over_limit_finding() -> dict[str, object]:
    """The finding produced by a description above the admission limit."""
    result = p4_oversize(_ctx("x" * 9000 + "\n" + _AC_BLOCK))
    assert result.status == "fail"
    assert result.blocking is True, "a description over the limit still BLOCKS"
    assert result.finding is not None
    return result.finding


def _signal_finding() -> dict[str, object]:
    """The advisory finding produced by a *short* description with too many ACs."""
    description = "## Acceptance Criteria\n" + "".join(
        f"- [ ] criterion {i}\n" for i in range(P4_AC_SOFT_CAP + 5)
    )
    result = p4_oversize(_ctx(description))
    assert result.status == "fail"
    assert result.blocking is False, "AC-count signals stay advisory"
    assert result.finding is not None
    assert len(description) < 8000, "this fixture must NOT trip the description limit"
    return result.finding


def test_over_limit_fix_coaches_concision_and_relocation() -> None:
    """AC1: concision and comment/session-log relocation are named remedies."""
    fix = str(_over_limit_finding()["suggested_fix"]).lower()
    assert "concise" in fix or "concision" in fix, fix
    assert "comment" in fix, fix
    assert "session log" in fix, fix


def test_over_limit_fix_names_both_halves_of_the_distinction() -> None:
    """AC2: context that STAYS vs narration that MOVES — not merely "be shorter"."""
    fix = str(_over_limit_finding()["suggested_fix"]).lower()
    # Context that must stay.
    for kept in ("problem", "constraint", "acceptance criteri"):
        assert kept in fix, f"missing context-that-stays term {kept!r}: {fix}"
    # Narration that should move.
    for moved in ("investigation", "rationale"):
        assert moved in fix, f"missing narration-that-moves term {moved!r}: {fix}"


def test_over_limit_fix_points_at_the_packaged_guide() -> None:
    """AC3: point at `rebar explain plan`, do not restate the guidance."""
    fix = str(_over_limit_finding()["suggested_fix"])
    assert "rebar explain plan" in fix, fix


def test_over_limit_fix_does_not_make_splitting_the_only_remedy() -> None:
    """The regression itself: splitting must not be the sole suggestion."""
    fix = str(_over_limit_finding()["suggested_fix"])
    assert fix != (
        "Reduce the description to at most 8000 characters, usually by splitting "
        "independent work into coherent child tickets."
    )


def test_decomposition_advice_survives_on_the_signal_branch() -> None:
    """AC4a: decomposition still coached where a unit is genuinely too large."""
    finding = _signal_finding()
    fix = str(finding["suggested_fix"]).lower()
    assert "child ticket" in fix or "decompos" in fix, fix


def test_the_two_branches_are_distinguishable() -> None:
    """AC4b: the over-length-prose case and the too-large-work case differ."""
    over = str(_over_limit_finding()["suggested_fix"])
    signal = str(_signal_finding()["suggested_fix"])
    assert over != signal
    # The advisory branch must not tell a short-description author to shorten it.
    assert "character" not in signal.lower(), signal


def test_admission_limit_is_unchanged_at_8000() -> None:
    """AC5: this ticket does not move the limit."""
    result = p4_oversize(_ctx("x" * 9000 + "\n" + _AC_BLOCK))
    assert result.coverage["desc_limit_chars"] == 8000
