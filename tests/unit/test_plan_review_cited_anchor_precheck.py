"""Warn-only cited-anchor pre-check before re-reviewing an unaddressed BLOCK (task ccba).

The contract under test is as much about what the pre-check does NOT do as what it does:
it warns, and then the review runs anyway. Every test here that exercises the warning also
pins that the caller is handed a plain metrics record rather than a verdict, because the
moment this check can decline a review it has broken its approved scope.
"""

from __future__ import annotations

import logging

import pytest

from rebar.llm.plan_review import cited_anchor

DESCRIPTION = (
    "## Plan\n\n"
    "Add a deterministic pre-check that compares cited anchors before a re-review.\n"
    "The acceptance criteria are verified by a unit test module in tests/unit.\n"
)

# A quote long enough to clear MIN_ANCHOR_CHARS and specific enough to be a real citation.
ANCHOR = "compares cited anchors before a re-review"


class _Ctx:
    """The slice of the plan-review context the pre-check reads."""

    def __init__(self, description: str = DESCRIPTION, ticket_type: str = "task") -> None:
        self.ticket_id = "ccba-3b6a-4241-4f59"
        self.ticket_type = ticket_type
        self.title = "Warn-only cited-anchor pre-check"
        self.description = description


def _blocking_finding(**over):
    finding = {
        "id": "AC-TESTABILITY",
        "decision": "block",
        "criteria": ["P6.ac-quality"],
        "finding": "The acceptance criteria are not independently verifiable.",
        "evidence": [ANCHOR],
    }
    finding.update(over)
    return finding


def _sidecar(findings, *, description_digest="digest-unchanged", verdict="BLOCK"):
    return {
        "schema": "plan_review_result_v2",
        "verdict": verdict,
        "ticket_type": "task",
        "findings": findings,
        "material_parts": {"description": [description_digest, len(DESCRIPTION)]},
    }


@pytest.fixture
def wire(monkeypatch):
    """Point the pre-check at a synthetic prior sidecar and a synthetic current fingerprint.

    ``material_components`` is patched rather than computed so a test can move the
    description component independently of the description text, which is exactly the
    axis the pre-check gates on.
    """

    def _wire(prior, *, current_digest="digest-unchanged"):
        monkeypatch.setattr(
            "rebar.llm.plan_review.sidecar.latest_review_result",
            lambda ticket_id, *, repo_root=None: prior,
        )
        monkeypatch.setattr(
            "rebar.llm.plan_review.material_diff.material_components",
            lambda ctx, **kw: {"description": (current_digest, len(ctx.description))},
        )

    return _wire


def test_warns_and_still_runs_when_cited_text_untouched(wire, caplog):
    """AC1: an unrevised plan warns, names the persisting criteria, and does NOT block."""
    wire(_sidecar([_blocking_finding()]))
    with caplog.at_level(logging.WARNING, logger="rebar.llm.plan_review.cited_anchor"):
        result = cited_anchor.precheck("ccba-3b6a-4241-4f59", _Ctx(), repo_root=None)

    assert result["cited_anchor_warning"] is True
    assert result["matched_anchors"] == 1
    assert result["findings"] == [{"id": "AC-TESTABILITY", "criteria": ["P6.ac-quality"]}]

    message = caplog.text
    assert "cited-anchor pre-check" in message
    assert "AC-TESTABILITY" in message
    assert "P6.ac-quality" in message
    assert "likely re-block" in message
    # WARN-ONLY: the pre-check hands back a metrics record, never a verdict that could
    # short-circuit the review the way reuse.verdict_reuse does.
    assert "verdict" not in result


def test_no_warning_when_the_cited_text_was_edited(wire, caplog):
    """AC2: once the description component moves, the review may learn something -> silent."""
    wire(_sidecar([_blocking_finding()]), current_digest="digest-revised")
    with caplog.at_level(logging.WARNING, logger="rebar.llm.plan_review.cited_anchor"):
        result = cited_anchor.precheck("ccba-3b6a-4241-4f59", _Ctx(), repo_root=None)

    assert result["cited_anchor_warning"] is False
    assert caplog.text == ""


@pytest.mark.parametrize(
    ("evidence", "why"),
    [
        ([], "a structural finding quoting nothing"),
        (["AC 1"], "an anchor below MIN_ANCHOR_CHARS"),
        (["a paraphrase that never appears in the plan text at all"], "paraphrased evidence"),
    ],
)
def test_unmatchable_anchors_never_warn_alone(wire, caplog, evidence, why):
    """AC3: unmatchable anchors are dropped, never treated as untouched."""
    wire(_sidecar([_blocking_finding(evidence=evidence)]))
    with caplog.at_level(logging.WARNING, logger="rebar.llm.plan_review.cited_anchor"):
        result = cited_anchor.precheck("ccba-3b6a-4241-4f59", _Ctx(), repo_root=None)

    assert result["cited_anchor_warning"] is False, why
    assert caplog.text == ""


def test_one_matched_anchor_is_enough_alongside_unmatchable_ones(wire):
    """A structural finding sitting beside a quoting one must not suppress the warning."""
    wire(_sidecar([_blocking_finding(evidence=[]), _blocking_finding(id="SCOPE")]))
    result = cited_anchor.precheck("ccba-3b6a-4241-4f59", _Ctx(), repo_root=None)

    assert result["cited_anchor_warning"] is True
    assert [f["id"] for f in result["findings"]] == ["SCOPE"]


def test_requoted_anchor_still_matches_across_whitespace(wire):
    """A finder that rewrapped its quote still matches text that never moved."""
    wire(_sidecar([_blocking_finding(evidence=["compares cited anchors\n   before a re-review"])]))
    assert cited_anchor.precheck("x", _Ctx(), repo_root=None)["cited_anchor_warning"] is True


@pytest.mark.parametrize(
    ("prior", "why"),
    [
        (None, "no prior review at all"),
        ("PASS_VERDICT", "the prior verdict passed"),
        ("NO_BLOCKING", "a BLOCK payload carrying no blocking findings"),
        ("NO_PARTS", "a pre-94a3 sidecar with no recorded components"),
    ],
)
def test_silent_without_a_usable_prior_block(wire, caplog, prior, why):
    payloads = {
        "PASS_VERDICT": _sidecar([_blocking_finding()], verdict="PASS"),
        "NO_BLOCKING": _sidecar([_blocking_finding(decision="advisory")]),
        "NO_PARTS": {**_sidecar([_blocking_finding()]), "material_parts": None},
    }
    wire(payloads.get(prior) if isinstance(prior, str) else prior)
    with caplog.at_level(logging.WARNING, logger="rebar.llm.plan_review.cited_anchor"):
        result = cited_anchor.precheck("ccba-3b6a-4241-4f59", _Ctx(), repo_root=None)

    assert result["cited_anchor_warning"] is False, why
    assert caplog.text == ""


def test_read_failure_degrades_to_no_warning(monkeypatch):
    """Fail-safe: the pre-check must never propagate an error into a review."""

    def _boom(ticket_id, *, repo_root=None):
        raise RuntimeError("unreadable sidecar")

    monkeypatch.setattr("rebar.llm.plan_review.sidecar.latest_review_result", _boom)
    result = cited_anchor.precheck("ccba-3b6a-4241-4f59", _Ctx(), repo_root=None)

    assert result["cited_anchor_warning"] is False


@pytest.mark.parametrize("warned", [True, False])
def test_metrics_flag_recorded_on_every_review(warned):
    """AC4: the sidecar metrics block carries the flag whether or not it fired."""
    verdict: dict = {}
    cited_anchor.record_metrics(verdict, {"cited_anchor_warning": warned})

    assert verdict["coverage"]["metrics"]["precheck"]["cited_anchor_warning"] is warned


def test_metrics_flag_preserves_existing_metrics():
    """Stamping must not clobber the per-pass latency/cost metrics already recorded."""
    verdict = {"coverage": {"metrics": {"llm_ms": 1234, "llm_calls": 3}, "llm_ran": True}}
    cited_anchor.record_metrics(verdict, {"cited_anchor_warning": True})

    metrics = verdict["coverage"]["metrics"]
    assert metrics["llm_ms"] == 1234 and metrics["llm_calls"] == 3
    assert metrics["precheck"]["cited_anchor_warning"] is True
    assert verdict["coverage"]["llm_ran"] is True


def test_metrics_flag_defaults_to_false_when_precheck_absent():
    """A None record (a path that never computed one) still measures as a False."""
    verdict: dict = {}
    cited_anchor.record_metrics(verdict, None)

    assert verdict["coverage"]["metrics"]["precheck"]["cited_anchor_warning"] is False


def test_record_metrics_repairs_non_dict_coverage():
    """Best-effort: a malformed carrier is replaced, never raised on."""
    verdict = {"coverage": "not-a-dict"}
    cited_anchor.record_metrics(verdict, {"cited_anchor_warning": True})

    assert verdict["coverage"]["metrics"]["precheck"]["cited_anchor_warning"] is True
