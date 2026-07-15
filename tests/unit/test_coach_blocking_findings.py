"""Pass-4 coaching over BLOCKING findings (story 8086): blocking + advisory findings both get
move-registry coaching with a decision tag and guide_url; the reviewer/writer separation
(locked moves, deterministic prose, subject validator) is unchanged."""

from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

import rebar
from rebar.llm.review_kernel.coach import coach as run_coach
from rebar.llm.review_kernel.coach import coach_listing

_REGISTRY = {
    "spike": {"name": "Spike", "template": "Run a quick spike on {subject}."},
}


def _finding(fid: str, criteria: list[str]) -> dict:
    return {"id": fid, "criteria": criteria, "finding": f"finding {fid}"}


def _pick_all(instructions: str, applicable: dict) -> list[dict]:
    # one note per listed finding id, echoing the listing (deterministic offline "LLM")
    ids = [
        line.split("id=")[1].split(" ")[0] for line in instructions.splitlines() if "id=" in line
    ]
    return [{"move_id": "spike", "subject": "the risky seam", "finding_refs": [i]} for i in ids]


class TestKernelCoachWidening:
    def test_mixed_block_and_advisory_yield_tagged_entries(self):
        notes = run_coach(
            [_finding("fa", ["E1"])],
            _REGISTRY,
            pick=_pick_all,
            blocking=[_finding("fb", ["F1"])],
        )
        by_ref = {n["finding_refs"][0]: n for n in notes}
        assert by_ref["fb"]["decision"] == "block"
        assert by_ref["fa"]["decision"] == "advisory"
        # blocking listed first in the coachable union
        assert list(by_ref) and notes[0]["finding_refs"] == ["fb"]

    def test_block_only_zero_advisory_still_coaches(self):
        # The motivating case: a BLOCK verdict with zero advisories previously got no coaching.
        notes = run_coach([], _REGISTRY, pick=_pick_all, blocking=[_finding("fb", ["F1"])])
        assert len(notes) == 1
        assert notes[0]["decision"] == "block"
        assert notes[0]["coaching"] == "Run a quick spike on the risky seam."

    def test_no_findings_makes_no_pick_call(self):
        calls = []

        def _pick(instructions, applicable):
            calls.append(1)
            return []

        assert run_coach([], _REGISTRY, pick=_pick) == []
        assert not calls

    def test_subject_validator_applies_to_blocking_notes(self):
        def _bad_pick(instructions, applicable):
            return [{"move_id": "spike", "subject": "add a retry loop", "finding_refs": ["fb"]}]

        notes = run_coach([], _REGISTRY, pick=_bad_pick, blocking=[_finding("fb", ["F1"])])
        assert notes == []  # imperative subject rejected — reviewer never writes the plan

    def test_listing_header_is_decision_neutral(self):
        listing = coach_listing([_finding("fa", ["E1"])], _REGISTRY)
        assert "## Surviving findings (by id)" in listing
        assert "advisory findings" not in listing.lower()


@pytest.fixture
def rebar_repo(tmp_path, monkeypatch):
    """A self-contained initialized rebar repo (this unit dir has no shared fixture)."""
    repo = Path(tmp_path) / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


class TestWorkflowOps:
    def test_coach_inputs_emits_coachable_union(self, rebar_repo):
        from rebar.llm.plan_review import workflow_ops
        from rebar.llm.workflow.executor import StepContext

        tid = rebar.create_ticket(
            "task",
            "coach inputs",
            description="A plan body long enough to assemble context for the coach step.\n\n"
            "## Acceptance Criteria\n- [ ] a check\n",
            repo_root=str(rebar_repo),
        )
        ctx = StepContext(
            run_id="r",
            step_id="s",
            kind="op",
            step={},
            inputs={
                "ticket_id": tid,
                "surviving": [_finding("fa", ["E1"])],
                "blocking": [_finding("fb", ["F1"])],
            },
            workflow={},
            repo_root=str(rebar_repo),
        )
        out = workflow_ops.plan_review_coach_inputs(ctx)
        assert [f["id"] for f in out["findings"]] == ["fb", "fa"]
        assert "id=fb" in out["instructions"] and "id=fa" in out["instructions"]

    def test_plan_review_coach_renders_blocking_note_with_decision(self, rebar_repo):
        from rebar.llm.plan_review import workflow_ops
        from rebar.llm.workflow.executor import StepContext

        ctx = StepContext(
            run_id="r",
            step_id="s",
            kind="op",
            step={},
            workflow={},
            inputs={
                "ticket_id": "t",
                "canonical_id": "t",
                "ticket_type": "task",
                "blocking": [_finding("fb", ["F1"])],
                "surfaced": [],
                "overflow": [],
                "indeterminate": [],
                "dropped": [],
                "notes": [
                    {"move_id": "1", "subject": "the eligibility seam", "finding_refs": ["fb"]}
                ],
                "det_coverage": {},
                "routing": {},
            },
            repo_root=str(rebar_repo),
        )
        verdict = workflow_ops.plan_review_coach(ctx)
        assert verdict["verdict"] == "BLOCK"
        blocking_notes = [c for c in verdict.get("coaching", []) if c.get("decision") == "block"]
        assert blocking_notes and blocking_notes[0]["finding_refs"] == ["fb"]
        # WS10 deep-link: the block note anchors on its finding's criterion
        assert blocking_notes[0]["guide_url"].endswith("#f1")


class TestYamlArmIdentity:
    def test_both_arms_coach_blocks_are_binding_identical(self):
        from importlib import resources

        text = (resources.files("rebar.llm.workflow") / "gates" / "plan-review.yaml").read_text()
        doc = yaml.safe_load(text)

        def _coach_steps(steps, found):
            for st in steps or []:
                if isinstance(st, dict):
                    if st.get("id") in ("coach_inputs", "coach_gate", "coach_notes", "coach"):
                        found.setdefault(st["id"], []).append(st)
                    br = st.get("branch") or {}
                    _coach_steps(br.get("then"), found)
                    _coach_steps(br.get("else"), found)
            return found

        found: dict = {}
        _coach_steps(doc.get("steps", []), found)
        # coach_inputs and coach_gate exist once per verify arm; their bindings must match
        for sid in ("coach_inputs", "coach_gate"):
            arms = found.get(sid, [])
            assert len(arms) == 2, f"{sid}: expected 2 arms, got {len(arms)}"
            assert arms[0].get("with") == arms[1].get("with")
            assert (arms[0].get("branch") or {}).get("when") == (arms[1].get("branch") or {}).get(
                "when"
            )


class TestSidecarAndCli:
    def test_coaching_decision_survives_build_payload(self):
        from rebar.llm.plan_review.sidecar import build_payload

        verdict = {
            "verdict": "BLOCK",
            "ticket_id": "t",
            "blocking": [_finding("fb", ["F1"])],
            "coaching": [
                {
                    "move_id": "spike",
                    "move_name": "Spike",
                    "subject": "s",
                    "finding_refs": ["fb"],
                    "coaching": "Run a quick spike on s.",
                    "decision": "block",
                    "guide_url": "https://x/guide#f1",
                }
            ],
        }
        payload = build_payload(verdict)
        assert payload["coaching"][0].get("decision") == "block"

    def test_cli_renders_blocking_coaching_line_with_guide_url(self):
        from rebar._cli import _llm_commands

        verdict = {
            "verdict": "BLOCK",
            "ticket_id": "t",
            "blocking": [_finding("fb", ["F1"])],
            "coaching": [
                {
                    "move_id": "spike",
                    "coaching": "Run a quick spike on the eligibility seam.",
                    "finding_refs": ["fb"],
                    "decision": "block",
                    "guide_url": "https://x/guide#f1",
                }
            ],
        }
        out = io.StringIO()
        with redirect_stdout(out):
            _llm_commands._render_plan_review_text(verdict)
        text = out.getvalue()
        assert "Run a quick spike on the eligibility seam." in text
        assert "https://x/guide#f1" in text
