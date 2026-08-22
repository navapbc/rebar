"""Step 0 — Orient must precede Phase 1 in the rebar-debug skill (story a4d7-ee6a-99ba-423e).

Two halves are pinned, and the second is the one a plain "the text exists" test would miss.

The rule must be PRESENT: a Step 0 section that sweeps the tracker across *all* statuses,
establishes the reported-against floor, and confirms the worktree is current.

And the broken mechanism must be GONE: the floor is derived from ``created_at``, which the
ticket schema documents as an integer of nanoseconds. ``git --before=`` accepts an approxidate
or ``@<unix-seconds>`` and silently mis-reads a raw 19-digit nanosecond value as a far-future
seconds count, returning the tip of ``main`` — the exact wrong answer for a *floor*. So the
skill must spell out the nanosecond-to-seconds conversion and must not carry the raw form.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "examples" / "agent-skills" / "rebar-debug" / "SKILL.md"


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


# ── the step exists, and it is positioned before Phase 1 ────────────────────────


def _heading(prefix: str) -> tuple[int, str]:
    """The sole heading line starting with *prefix*, as (1-based line number, text).

    A named lookup rather than a bare ``next``: a renamed or removed heading should fail with
    the heading it looked for, not an opaque ``StopIteration``.
    """
    hits = [(n, line) for n, line in enumerate(_skill().splitlines(), 1) if line.startswith(prefix)]
    assert len(hits) == 1, f"expected exactly one heading starting {prefix!r}, found {len(hits)}"
    return hits[0]


def test_step_0_precedes_phase_1() -> None:
    """Ordering is the whole point: orienting after gathering evidence is worthless."""
    step0, _ = _heading("# Step 0 — Orient")
    phase1, _ = _heading("# Phase 1")
    assert step0 < phase1, f"Step 0 (line {step0}) must precede Phase 1 (line {phase1})"


def test_step_0_applies_to_the_fast_path_too() -> None:
    """A duplicate is likeliest to slip through on the path that skips the dossier."""
    _, heading = _heading("# Step 0 — Orient")
    assert "fast path" in heading


def test_step_0_names_all_three_actions() -> None:
    body = _skill()
    assert 'rebar search "<symptom words>"' in body
    assert "rebar list --type bug --status open,in_progress" in body
    assert 'git rev-list -1 --before="@' in body
    assert "git rev-parse origin/main" in body


# ── the tracker sweep is not blind to the closed majority ───────────────────────


def test_sweep_covers_closed_tickets() -> None:
    """759 of 764 bug tickets are closed; an open-only sweep sees almost nothing."""
    assert "including closed tickets" in _skill()


def test_duplicate_outcome_is_a_recorded_link_or_resume() -> None:
    body = _skill()
    assert "rebar link <new> <existing> duplicates" in body
    assert "resume the existing" in body


def test_the_choice_between_link_and_resume_has_a_decision_rule() -> None:
    """Two mutually exclusive outcomes with no rule is a coin toss, not a protocol."""
    body = _skill()
    assert "Open or in progress" in body
    assert "resume that ticket in place" in body
    assert "Closed, and the behavior is back" in body
    assert "regression" in body


def test_step_0_link_is_pre_approved_in_the_carve_out() -> None:
    """Otherwise Step 0 mandates an action the same file gates behind user approval."""
    body = _skill()
    carve_out = body.split("**Carve-out — the project's own tracker.**", 1)
    assert len(carve_out) == 2, "the own-tracker carve-out paragraph is missing"
    paragraph = carve_out[1].split("\n\n", 1)[0]
    assert "Step-0 duplicates link" in paragraph


# ── the floor mechanism is stated correctly, and the broken form is absent ──────


def test_nanosecond_conversion_is_spelled_out() -> None:
    body = _skill()
    assert "nanoseconds" in body, "the units of created_at must be stated"
    # Pin the divide inside the runnable command, not merely somewhere in the prose:
    # a wrong divisor there is silently wrong, which is the defect this step exists to stop.
    assert 'git rev-list -1 --before="@$(( <created_at> / 1000000000 ))" origin/main' in body


def test_every_before_argument_uses_the_converted_form() -> None:
    """A positive oracle, so a raw value reintroduced in ANY spelling fails.

    The pre-remediation form silently returned the tip of main instead of a floor. Checking
    two literal spellings could not tell "the broken form is gone" from "it came back spelled
    differently", so every ``--before=`` in the file must carry git's ``@<unix-seconds>`` form.
    """
    # ``[^\s`]`` so the bare ``--before=`` in the surrounding prose (a backtick-quoted
    # mention with no argument) is not mistaken for an invocation.
    args = [a for a in re.findall(r"--before=([^\s`]*)", _skill()) if a]
    assert args, "the floor command must be present at all"
    for arg in args:
        assert arg.lstrip("\"'").startswith("@"), (
            f"--before={arg} does not use git's @<unix-seconds> form; a raw nanosecond "
            "value is read as far-future seconds and silently returns the tip of main"
        )


def test_no_ticket_rebar_version_field_is_referenced() -> None:
    """No such field exists on a rebar ticket; reading it yields nothing, silently."""
    assert "rebar-version" not in _skill()


def test_floor_is_described_as_a_floor_not_the_exact_build() -> None:
    body = _skill()
    assert "reported on or after <sha>" in body


def test_unknown_version_case_is_explicit() -> None:
    """Without this the agent defaults to main — the stale-tree bug in disguise."""
    assert "reported-against version unknown" in _skill()


def test_diff_range_to_main_is_recorded() -> None:
    assert "git log --oneline <floor-sha>..origin/main" in _skill()
