"""Project-level dogfood coverage for both optional plan-review policy gates."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import rebar
from rebar import config, signing
from rebar.llm.plan_review import attest
from rebar.llm.runner import FakeRunner

PROJECT_ROOT = Path(__file__).parents[3]

_DESCRIPTION = (
    "A complete lifecycle plan with enough detail for deterministic review.\n\n"
    "## Acceptance Criteria\n- [ ] the public transition is observable\n"
    "- [ ] failures preserve the prior ticket status\n\n"
    "## Why\nDogfood the policy.\n## What\nExercise the real CLI.\n## Scope\nOne fixture.\n"
)


class _PassRunner(FakeRunner):
    name = "fake"

    def run(self, req):  # type: ignore[override]
        from rebar.llm import findings

        if req.mode == "text":
            return {"text": "fixture summary", "runner": self.name, "model": None}
        if req.output_schema == "plan_review_findings":
            payload = {"analysis": "", "findings": []}
        elif req.output_schema == "plan_review_prerequisite_coverage":
            ids = re.findall(r'<prerequisite id="([^"]+)">', req.instructions)
            payload = {
                "records": [
                    {
                        "prerequisite_id": prerequisite_id,
                        "disposition": "consistent",
                        "findings": [],
                        "reason_code": None,
                    }
                    for prerequisite_id in ids
                ]
            }
        elif req.output_schema == "plan_review_verification":
            indexes = [int(value) for value in re.findall(r"finding index (\d+)", req.instructions)]
            payload = {"verifications": [] if not indexes else []}
        elif req.output_schema == "plan_review_coach":
            payload = {"notes": []}
        else:
            payload = {"analysis": "", "findings": []}
        return {
            **findings.validate_structured(payload, req.output_schema),
            "runner": self.name,
            "model": None,
            "trace_id": None,
        }


def _assert_project_policies_enabled() -> None:
    loaded = config.load_config(str(PROJECT_ROOT))
    assert loaded.verify.enforce_plan_material_pins is True
    assert loaded.verify.require_plan_review_for_close is True


def _enable_fixture(repo: Path) -> None:
    (repo / "rebar.toml").write_text(
        """[verify]
require_plan_review_for_claim = true
enforce_plan_material_pins = true
require_plan_review_for_close = true
require_completion_verification_for_close = false

[sync]
push = "off"
pull = "off"
""",
        encoding="utf-8",
    )


def _cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def _commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "fixture baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _ticket(repo: Path, title: str) -> str:
    return rebar.create_ticket("task", title, description=_DESCRIPTION, repo_root=str(repo))


def _legacy_plan_attestation(repo: Path, ticket_id: str) -> None:
    material = attest.current_material_fingerprint(ticket_id, repo_root=str(repo))
    assert material is not None
    manifest = attest.build_manifest(
        {"verdict": "PASS", "ticket_id": ticket_id, "coverage": {"counts": {}}},
        material=material,
        regver=attest.registry_version(str(repo)),
        review_phase="planning",
        pins=(),
    )
    signing.sign_manifest(ticket_id, manifest, kind="plan-review", repo_root=str(repo))


def test_project_enables_both_policies_while_library_defaults_remain_off() -> None:
    _assert_project_policies_enabled()
    defaults = config.Config.from_mapping(None)
    assert defaults.verify.enforce_plan_material_pins is False
    assert defaults.verify.require_plan_review_for_close is False


def test_legacy_unpinned_attestation_claims_through_real_cli(rebar_repo: Path) -> None:
    _assert_project_policies_enabled()
    _commit(rebar_repo)
    _enable_fixture(rebar_repo)
    ticket_id = _ticket(rebar_repo, "legacy unpinned")
    _legacy_plan_attestation(rebar_repo, ticket_id)

    state = rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))
    before = state["status"]
    result = _cli(rebar_repo, "claim", ticket_id, "--assignee=fixture")
    after = rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))["status"]

    assert before == "open"
    assert result.returncode == 0, result.stderr
    assert after == "in_progress"


def test_current_pins_and_execution_review_close_through_real_cli(rebar_repo: Path) -> None:
    _assert_project_policies_enabled()
    _commit(rebar_repo)
    _enable_fixture(rebar_repo)
    prerequisite = _ticket(rebar_repo, "canonical prerequisite")
    ticket_id = _ticket(rebar_repo, "current pinned plan")
    rebar.link(ticket_id, prerequisite, "depends_on", repo_root=str(rebar_repo))

    planning = rebar.llm.review_plan(ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo))
    assert planning["verdict"] == "PASS"
    claimed = _cli(rebar_repo, "claim", ticket_id, "--assignee=fixture")
    assert claimed.returncode == 0, claimed.stderr

    execution = rebar.llm.review_plan(
        ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
    )
    assert execution["verdict"] == "PASS"
    signing.sign_manifest(
        ticket_id,
        ["completion-verifier: PASS", f"ticket: {ticket_id}"],
        kind="completion-verifier",
        repo_root=str(rebar_repo),
    )

    before = rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))["status"]
    result = _cli(rebar_repo, "transition", ticket_id, "in_progress", "closed")
    after = rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))["status"]

    assert before == "in_progress"
    assert result.returncode == 0, result.stderr
    assert after == "closed"


def test_drifted_related_pin_blocks_claim_with_canonical_target_through_real_cli(
    rebar_repo: Path,
) -> None:
    _assert_project_policies_enabled()
    _commit(rebar_repo)
    _enable_fixture(rebar_repo)
    prerequisite = _ticket(rebar_repo, "canonical prerequisite")
    ticket_id = _ticket(rebar_repo, "drifted pinned plan")
    rebar.link(ticket_id, prerequisite, "depends_on", repo_root=str(rebar_repo))
    assert (
        rebar.llm.review_plan(ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo))["verdict"]
        == "PASS"
    )
    rebar.edit_ticket(prerequisite, description="material changed", repo_root=str(rebar_repo))

    before = rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))["status"]
    result = _cli(rebar_repo, "claim", ticket_id, "--assignee=fixture")
    after = rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))["status"]

    assert before == "open"
    assert result.returncode == 1
    assert "stale-pin-drift" in result.stderr
    assert prerequisite in result.stderr
    assert after == "open"


def test_planning_phase_review_blocks_close_until_execution_review_through_real_cli(
    rebar_repo: Path,
) -> None:
    _assert_project_policies_enabled()
    _commit(rebar_repo)
    _enable_fixture(rebar_repo)
    ticket_id = _ticket(rebar_repo, "planning phase only")
    planning = rebar.llm.review_plan(ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo))
    assert planning["verdict"] == "PASS"
    assert (
        "review-phase: planning"
        in rebar.verify_signature(ticket_id, kind="plan-review", repo_root=str(rebar_repo))[
            "manifest"
        ]
    )
    assert _cli(rebar_repo, "claim", ticket_id, "--assignee=fixture").returncode == 0

    state = rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))
    before = state["status"]
    assert config.load_config(str(rebar_repo)).verify.require_plan_review_for_close is True
    from rebar._commands import gates

    check = gates.close_plan_review_gate_check(ticket_id, state, repo_root=str(rebar_repo))
    assert check["ok"] is False, check
    assert check["verdict"] == "incompatible-phase"
    result = _cli(rebar_repo, "transition", ticket_id, "in_progress", "closed")
    after = rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))["status"]

    assert before == "in_progress"
    assert result.returncode == 1
    assert "incompatible-phase" in result.stderr
    assert "review-plan" in result.stderr
    assert after == "in_progress"
