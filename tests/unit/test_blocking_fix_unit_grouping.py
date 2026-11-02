"""Blocking fix-unit grouping (bug longish-ripe-snowyowl, superseding story 5e64's lexical key):
ONE set-level grouping call over a verdict's BLOCKING findings stamps the findings that state one
defect as one group. Nothing leaves ``verdict["blocking"]``, the sidecar stays lossless, only the
CLI renderer collapses a group to its primary, and any failure stamps nothing (fail open)."""

from __future__ import annotations

import copy
import io
import types
from contextlib import redirect_stdout

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import _group_blocking_fix_units
from rebar.llm.plan_review.sidecar import build_payload, fix_unit_key
from rebar.llm.runner import FakeRunner

pytestmark = pytest.mark.unit

_CTX = types.SimpleNamespace(plan_text="the plan", state={})
_CFG = LLMConfig(runner="fake")
_STAMPS = ("group_id", "is_primary", "group_criteria")


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
    """One defect reworded by five criteria — the shape no lexical key ever matched."""
    texts = [
        "`ContentFilterError` is listed in the failure matrix but no such symbol exists",
        "the plan's edit-set cites ContentFilterError, which is absent from the tree",
        "the approach relies on a ContentFilterError class that is not defined anywhere",
        "testing names ContentFilterError as a raised error; the codebase has no such type",
        "failure matrix row references a nonexistent ContentFilterError exception",
    ]
    return [
        _finding(f"f{i}", [c], location=f"§{i}", text=t, priority=priority)
        for i, (c, t) in enumerate(zip(["E4", "G1G2", "G6", "T1", "T8"], texts, strict=True))
    ]


def _verdict(blocking: list[dict]) -> dict:
    return {
        "verdict": "BLOCK",
        "ticket_id": "t",
        "blocking": blocking,
        "advisory": [],
        "coverage": {"counts": {"blocking": len(blocking)}},
    }


class _RecordingRunner:
    """Records every request and returns a canned structured payload (or raises)."""

    name = "fake"

    def __init__(self, structured: dict | None = None, *, error: Exception | None = None):
        self._structured = structured
        self._error = error
        self.requests: list = []

    def preflight(self) -> None:
        """Always ready."""

    def run(self, req) -> dict:
        self.requests.append(req)
        if self._error is not None:
            raise self._error
        return {**(self._structured or {}), "_usage": {"input_tokens": 7}}


def _group(verdict: dict, runner) -> None:
    _group_blocking_fix_units(verdict, ctx=_CTX, cfg=_CFG, runner=runner)


def _all_ids(n: int) -> list[str]:
    return [f"f{i}" for i in range(n)]


class TestFixUnitKey:
    """``fix_unit_key`` no longer drives grouping; the criterion-effectiveness eval harness
    still uses it as its cross-round match key."""

    def test_same_claim_different_criteria_share_key(self):
        a = _finding("f1", ["E4"], location="plan §2", text="symbol X does not exist")
        b = _finding("f2", ["T8"], location="plan §2", text="symbol X does not exist")
        assert fix_unit_key(a) == fix_unit_key(b)

    def test_different_location_or_claim_differ(self):
        base = _finding("f1", ["E4"], location="plan §2", text="symbol X does not exist")
        other = _finding("f3", ["E4"], location="plan §2", text="rollback steps missing entirely")
        assert fix_unit_key(base) != fix_unit_key(other)


class TestGrouping:
    def test_model_group_stamps_one_fix_unit(self):
        verdict = _verdict(_five_cocited())
        before = copy.deepcopy(verdict)
        runner = FakeRunner(structured={"groups": [{"defect": "d", "members": _all_ids(5)}]})
        _group(verdict, runner)
        blocking = verdict["blocking"]
        assert len(blocking) == 5  # stamp-only: nothing removed
        assert verdict["verdict"] == before["verdict"]
        assert all(f["decision"] == "block" for f in blocking)
        primaries = [f for f in blocking if f.get("is_primary")]
        assert len(primaries) == 1
        assert len({f["group_id"] for f in blocking}) == 1
        assert primaries[0]["group_criteria"] == ["E4", "G1G2", "G6", "T1", "T8"]
        assert verdict["coverage"]["fix_unit_grouping"]["status"] == "grouped"
        assert verdict["coverage"]["fix_unit_grouping"]["groups"] == 1
        assert verdict["coverage"]["counts"] == before["coverage"]["counts"]

    def test_only_named_members_are_grouped(self):
        verdict = _verdict(_five_cocited())
        runner = FakeRunner(structured={"groups": [{"defect": "d", "members": ["f0", "f3"]}]})
        _group(verdict, runner)
        grouped = [f["id"] for f in verdict["blocking"] if "group_id" in f]
        assert grouped == ["f0", "f3"]

    def test_group_id_is_deterministic_over_members(self):
        a, b = _verdict(_five_cocited()), _verdict(list(reversed(_five_cocited())))
        for v, members in ((a, ["f1", "f2"]), (b, ["f2", "f1"])):
            _group(v, FakeRunner(structured={"groups": [{"defect": "d", "members": members}]}))
        gid = {f["group_id"] for f in a["blocking"] if "group_id" in f}
        assert gid == {f["group_id"] for f in b["blocking"] if "group_id" in f}

    def test_one_call_sees_every_blocking_finding(self):
        verdict = _verdict(_five_cocited())
        runner = _RecordingRunner({"groups": []})
        _group(verdict, runner)
        assert len(runner.requests) == 1
        req = runner.requests[0]
        assert req.output_schema == "plan_review_fix_unit_groups"
        assert req.config.temperature == 0.0
        for fid in _all_ids(5):
            assert f"id={fid}" in req.instructions
        assert verdict["coverage"]["fix_unit_grouping"] == {
            "status": "no_groups",
            "groups": 0,
            "usage": {"input_tokens": 7},
        }
        assert all(k not in f for f in verdict["blocking"] for k in _STAMPS)

    @pytest.mark.parametrize("n", [0, 1])
    def test_fewer_than_two_blockers_make_no_call(self, n):
        verdict = _verdict(_five_cocited()[:n])
        verdict["advisory"] = _five_cocited()  # advisory findings never count
        before = copy.deepcopy(verdict)
        runner = _RecordingRunner({"groups": [{"defect": "d", "members": ["f0", "f1"]}]})
        _group(verdict, runner)
        assert runner.requests == []
        assert verdict == before

    def test_repeated_ids_make_no_call(self):
        blocking = _five_cocited()[:2]
        blocking[1]["id"] = blocking[0]["id"]
        verdict = _verdict(blocking)
        runner = _RecordingRunner({"groups": []})
        _group(verdict, runner)
        assert runner.requests == []


class TestFailOpen:
    """Any failure stamps nothing and never changes the verdict."""

    @pytest.mark.parametrize(
        "structured",
        [
            {"groups": [{"defect": "d", "members": ["f0", "nope"]}]},  # unknown id
            {"groups": [{"members": ["f0", "f1"]}, {"members": ["f1", "f2"]}]},  # overlap
            {"groups": "f0,f1"},  # malformed: not a list
            {"groups": [{"defect": "d", "members": "f0"}]},  # malformed: members not a list
            {"groups": [{"defect": "d", "members": ["f0", 3]}]},  # malformed: non-str id
            {"groups": ["f0"]},  # malformed: group not an object
        ],
    )
    def test_invalid_answer_stamps_nothing(self, structured):
        verdict = _verdict(_five_cocited())
        before = copy.deepcopy(verdict["blocking"])
        _group(verdict, _RecordingRunner(structured))
        assert verdict["blocking"] == before
        assert verdict["verdict"] == "BLOCK"
        assert verdict["coverage"]["fix_unit_grouping"]["status"] == "failed"

    def test_raising_call_stamps_nothing(self):
        verdict = _verdict(_five_cocited())
        before = copy.deepcopy(verdict["blocking"])
        _group(verdict, _RecordingRunner(error=RuntimeError("provider down")))
        assert verdict["blocking"] == before
        assert verdict["verdict"] == "BLOCK"
        assert verdict["coverage"]["fix_unit_grouping"] == {
            "status": "failed",
            "groups": 0,
            "usage": {},
        }

    def test_fake_runner_contract_rejection_stamps_nothing(self):
        # FakeRunner validates against the registered contract; a type-invalid answer raises
        # inside the runner and must fail open the same way.
        verdict = _verdict(_five_cocited())
        before = copy.deepcopy(verdict["blocking"])
        _group(verdict, FakeRunner(structured={"groups": [{"members": {"x": 1}}]}))
        assert verdict["blocking"] == before
        assert verdict["coverage"]["fix_unit_grouping"]["status"] == "failed"

    def test_single_member_groups_are_ignored(self):
        verdict = _verdict(_five_cocited())
        _group(verdict, FakeRunner(structured={"groups": [{"defect": "d", "members": ["f0"]}]}))
        assert all(k not in f for f in verdict["blocking"] for k in _STAMPS)
        assert verdict["coverage"]["fix_unit_grouping"]["status"] == "no_groups"


class TestPrimary:
    def test_primary_tiebreaks_are_deterministic(self):
        everyone = {"groups": [{"defect": "d", "members": _all_ids(5)}]}
        # equal priorities → alphabetically-first criteria entry wins; then lowest id
        verdict = _verdict(list(reversed(_five_cocited(priority=0.7))))
        _group(verdict, FakeRunner(structured=everyone))
        primary = next(f for f in verdict["blocking"] if f.get("is_primary"))
        assert primary["criteria"] == ["E4"]
        # missing priority sorts as 0.0 (never wins over a scored sibling)
        members = _five_cocited()
        del members[0]["priority"]
        verdict = _verdict(members)
        _group(verdict, FakeRunner(structured=everyone))
        primary = next(f for f in verdict["blocking"] if f.get("is_primary"))
        assert primary["id"] != "f0"


def _grouped_verdict() -> dict:
    verdict = _verdict(_five_cocited())
    _group(verdict, FakeRunner(structured={"groups": [{"defect": "d", "members": _all_ids(5)}]}))
    return verdict


class TestSidecarPersistence:
    def test_build_payload_retains_all_grouped_findings(self):
        payload = build_payload(_grouped_verdict())
        rows = [f for f in payload["findings"] if f.get("decision") == "block"]
        assert len(rows) == 5
        assert len({f["group_id"] for f in rows}) == 1
        assert sum(1 for f in rows if f.get("is_primary")) == 1


class TestCliGroupRendering:
    def test_primary_rendered_with_co_criteria_suffix(self):
        from rebar._cli import _llm_commands

        out = io.StringIO()
        with redirect_stdout(out):
            _llm_commands._render_plan_review_text(_grouped_verdict())
        text = out.getvalue()
        assert text.count("[BLOCK") == 1  # folded members suppressed
        assert "(+4 co-criteria:" in text
        for c in ["G1G2", "G6", "T1", "T8"]:
            assert c in text

    def test_ungrouped_findings_render_unchanged(self):
        from rebar._cli import _llm_commands

        verdict = _verdict(
            [_finding("f1", ["E4"], location="plan §2", text="symbol X does not exist")]
        )
        _group(verdict, FakeRunner(structured={"groups": []}))
        out = io.StringIO()
        with redirect_stdout(out):
            _llm_commands._render_plan_review_text(verdict)
        assert "co-criteria" not in out.getvalue()
        assert "[BLOCK E4]" in out.getvalue()
