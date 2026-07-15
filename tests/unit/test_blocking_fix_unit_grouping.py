"""Blocking fix-unit grouping (story 5e64): one defect co-cited by N criteria is stamped as one
group — nothing leaves ``verdict["blocking"]``, the sidecar stays lossless, and only the CLI
renderer collapses a group to its primary."""

from __future__ import annotations

import io
from contextlib import redirect_stdout

from rebar.llm.plan_review import _group_blocking_fix_units
from rebar.llm.plan_review.sidecar import build_payload, fix_unit_key


def _finding(fid: str, criteria: list[str], *, location: str, text: str, priority=0.8) -> dict:
    return {
        "id": fid,
        "criteria": criteria,
        "location": location,
        "finding": text,
        "decision": "block",
        "priority": priority,
    }


def _five_cocited(priority=0.8) -> list[dict]:
    loc = "## What: the failure matrix, `ContentFilterError` row"
    text = "`ContentFilterError` is listed in the failure matrix but no such symbol exists"
    return [
        _finding(f"f{i}", [c], location=loc, text=text, priority=priority)
        for i, c in enumerate(["E4", "G1G2", "G6", "T1", "T8"])
    ]


class TestFixUnitKey:
    def test_same_claim_different_criteria_share_key(self):
        a = _finding("f1", ["E4"], location="plan §2", text="symbol X does not exist")
        b = _finding("f2", ["T8"], location="plan §2", text="symbol X does not exist")
        assert fix_unit_key(a) == fix_unit_key(b)

    def test_reword_tolerant_like_norm_id(self):
        a = _finding("f1", ["E4"], location="plan §2", text="the symbol X does not exist anywhere")
        b = _finding("f2", ["T8"], location="plan §2", text="symbol X does not exist")
        # token-set normalization: stop-tokens ("the") and order don't matter, real tokens do
        assert fix_unit_key(a) != fix_unit_key(b)  # "anywhere" is a significant token
        c = _finding("f3", ["T8"], location="plan §2", text="does not exist: symbol X")
        assert fix_unit_key(b) == fix_unit_key(c)

    def test_different_location_or_claim_differ(self):
        base = _finding("f1", ["E4"], location="plan §2", text="symbol X does not exist")
        other_loc = _finding(
            "f2", ["E4"], location="plan §9 rollback", text="symbol X does not exist"
        )
        other_claim = _finding(
            "f3", ["E4"], location="plan §2", text="rollback steps missing entirely"
        )
        assert fix_unit_key(base) != fix_unit_key(other_loc)
        assert fix_unit_key(base) != fix_unit_key(other_claim)


class TestGrouping:
    def test_five_cocited_findings_stamp_one_group(self):
        verdict = {"verdict": "BLOCK", "blocking": _five_cocited()}
        _group_blocking_fix_units(verdict)
        blocking = verdict["blocking"]
        assert len(blocking) == 5  # stamp-only: nothing removed
        assert verdict["verdict"] == "BLOCK"
        primaries = [f for f in blocking if f.get("is_primary")]
        assert len(primaries) == 1
        assert len({f["group_id"] for f in blocking}) == 1
        assert primaries[0]["group_criteria"] == ["E4", "G1G2", "G6", "T1", "T8"]

    def test_distinct_defects_not_grouped(self):
        verdict = {
            "verdict": "BLOCK",
            "blocking": [
                _finding("f1", ["E4"], location="plan §2", text="symbol X does not exist"),
                _finding("f2", ["F1"], location="AC3", text="the AC has no proving command"),
            ],
        }
        _group_blocking_fix_units(verdict)
        assert all("group_id" not in f for f in verdict["blocking"])

    def test_primary_tiebreaks_are_deterministic(self):
        # equal priorities → alphabetically-first criteria entry wins; then lowest id
        members = _five_cocited(priority=0.7)
        verdict = {"verdict": "BLOCK", "blocking": list(reversed(members))}
        _group_blocking_fix_units(verdict)
        primary = next(f for f in verdict["blocking"] if f.get("is_primary"))
        assert primary["criteria"] == ["E4"]
        # missing priority sorts as 0.0 (never wins over a scored sibling)
        members = _five_cocited()
        del members[0]["priority"]
        verdict = {"verdict": "BLOCK", "blocking": members}
        _group_blocking_fix_units(verdict)
        primary = next(f for f in verdict["blocking"] if f.get("is_primary"))
        assert primary["id"] != members[0]["id"]


class TestSidecarPersistence:
    def test_build_payload_retains_all_grouped_findings(self):
        verdict = {"verdict": "BLOCK", "ticket_id": "t", "blocking": _five_cocited()}
        _group_blocking_fix_units(verdict)
        payload = build_payload(verdict)
        rows = [f for f in payload["findings"] if f.get("decision") == "block"]
        assert len(rows) == 5
        assert len({f["group_id"] for f in rows}) == 1
        assert sum(1 for f in rows if f.get("is_primary")) == 1


class TestCliGroupRendering:
    def test_primary_rendered_with_co_criteria_suffix(self):
        from rebar._cli import _llm_commands

        verdict = {"verdict": "BLOCK", "ticket_id": "t", "blocking": _five_cocited()}
        _group_blocking_fix_units(verdict)
        out = io.StringIO()
        with redirect_stdout(out):
            _llm_commands._render_plan_review_text(verdict)
        text = out.getvalue()
        assert text.count("[BLOCK") == 1  # folded members suppressed
        assert "(+4 co-criteria:" in text
        for c in ["G1G2", "G6", "T1", "T8"]:
            assert c in text

    def test_ungrouped_findings_render_unchanged(self):
        from rebar._cli import _llm_commands

        verdict = {
            "verdict": "BLOCK",
            "ticket_id": "t",
            "blocking": [
                _finding("f1", ["E4"], location="plan §2", text="symbol X does not exist")
            ],
        }
        _group_blocking_fix_units(verdict)
        out = io.StringIO()
        with redirect_stdout(out):
            _llm_commands._render_plan_review_text(verdict)
        assert "co-criteria" not in out.getvalue()
        assert "[BLOCK E4]" in out.getvalue()
