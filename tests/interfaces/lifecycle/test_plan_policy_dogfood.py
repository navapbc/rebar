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


def _set_fixture_pin_enforcement(repo: Path, *, enabled: bool) -> None:
    text = (repo / "rebar.toml").read_text(encoding="utf-8")
    text = re.sub(
        r"(?m)^enforce_plan_material_pins = (?:true|false)$",
        f"enforce_plan_material_pins = {str(enabled).lower()}",
        text,
    )
    (repo / "rebar.toml").write_text(text, encoding="utf-8")


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


class _BoomRunner(FakeRunner):
    """Fails loudly if the review reaches the LLM — proves the not-claimable fast-fail
    returns before any LLM pass."""

    name = "boom"

    def run(self, req):  # type: ignore[override]
        raise AssertionError("LLM runner must not be invoked for a non-claimable ticket")


def _assert_not_claimable(result: dict, ticket_id: str) -> None:
    assert result["verdict"] == "INDETERMINATE"
    assert result["ticket_id"] == ticket_id
    assert result["coverage"]["llm_ran"] is False
    finding_ids = [f.get("id") for f in result.get("indeterminate", [])]
    assert "ticket-not-claimable" in finding_ids
    assert result.get("signature", {}).get("signed") is False


def test_open_ticket_blocked_by_open_prerequisite_fast_fails_without_llm(rebar_repo: Path) -> None:
    _enable_fixture(rebar_repo)
    _commit(rebar_repo)
    prerequisite = _ticket(rebar_repo, "open prerequisite")
    subject = _ticket(rebar_repo, "link-blocked subject")
    rebar.link(subject, prerequisite, "depends_on", repo_root=str(rebar_repo))

    result = rebar.llm.review_plan(subject, runner=_BoomRunner(), repo_root=str(rebar_repo))

    _assert_not_claimable(result, subject)
    assert prerequisite in result["indeterminate"][0]["blockers"]
    # No attestation was minted, so the claim gate still refuses to start work.
    claimed = _cli(rebar_repo, "claim", subject, "--assignee=fixture")
    assert claimed.returncode != 0


def test_force_reviews_a_link_blocked_ticket_past_the_gate(rebar_repo: Path) -> None:
    _enable_fixture(rebar_repo)
    _commit(rebar_repo)
    prerequisite = _ticket(rebar_repo, "open prerequisite")
    subject = _ticket(rebar_repo, "link-blocked subject")
    rebar.link(subject, prerequisite, "depends_on", repo_root=str(rebar_repo))

    result = rebar.llm.review_plan(
        subject, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
    )

    assert result["verdict"] == "PASS"
    assert result["coverage"]["llm_ran"] is True


def test_blocked_status_ticket_fast_fails_without_llm(rebar_repo: Path) -> None:
    _enable_fixture(rebar_repo)
    _commit(rebar_repo)
    subject = _ticket(rebar_repo, "paused subject")
    rebar.transition(subject, "open", "blocked", repo_root=str(rebar_repo))

    result = rebar.llm.review_plan(subject, runner=_BoomRunner(), repo_root=str(rebar_repo))

    _assert_not_claimable(result, subject)
    assert result["indeterminate"][0]["status"] == "blocked"


def test_idea_status_ticket_fast_fails_without_llm(rebar_repo: Path) -> None:
    _enable_fixture(rebar_repo)
    _commit(rebar_repo)
    idea_id = rebar.idea("undesigned idea", description=_DESCRIPTION, repo_root=str(rebar_repo))

    result = rebar.llm.review_plan(idea_id, runner=_BoomRunner(), repo_root=str(rebar_repo))

    _assert_not_claimable(result, idea_id)
    assert result["indeterminate"][0]["status"] == "idea"


def test_in_progress_ticket_with_open_blocker_is_not_fast_failed(rebar_repo: Path) -> None:
    # No claim gate here, so the subject can be claimed into in_progress directly while
    # its prerequisite stays open. A claimed ticket is worked in place, so drift/force
    # re-reviews must still run the LLM rather than being fast-failed.
    _commit(rebar_repo)
    prerequisite = _ticket(rebar_repo, "still-open prerequisite")
    subject = _ticket(rebar_repo, "in-progress subject")
    rebar.link(subject, prerequisite, "depends_on", repo_root=str(rebar_repo))
    rebar.claim(subject, assignee="fixture", repo_root=str(rebar_repo))
    assert rebar.show_ticket(subject, repo_root=str(rebar_repo))["status"] == "in_progress"

    result = rebar.llm.review_plan(subject, runner=_PassRunner(), repo_root=str(rebar_repo))

    assert result["verdict"] == "PASS"
    assert result["coverage"]["llm_ran"] is True


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

    # The subject is link-blocked by an open prerequisite, so a default review would
    # fast-fail as not-yet-claimable; force the planning review to exercise pinning.
    planning = rebar.llm.review_plan(
        ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
    )
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
    # Link-blocked by an open prerequisite: force the review past the not-claimable gate.
    assert (
        rebar.llm.review_plan(
            ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
        )["verdict"]
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


def test_natural_a_to_b_to_c_invalidation_requires_each_narrow_material_change(
    rebar_repo: Path,
) -> None:
    """A change propagates through reviewed pins only when each plan changes."""
    _assert_project_policies_enabled()
    _commit(rebar_repo)
    _enable_fixture(rebar_repo)
    plan_a = _ticket(rebar_repo, "transitive plan A")
    plan_b = _ticket(rebar_repo, "transitive plan B")
    plan_c = _ticket(rebar_repo, "transitive plan C")
    rebar.link(plan_b, plan_a, "depends_on", repo_root=str(rebar_repo))
    rebar.link(plan_c, plan_b, "depends_on", repo_root=str(rebar_repo))
    # B and C are each link-blocked by an open prerequisite; force past the
    # not-claimable gate to establish the pins this transitive-drift test relies on.
    assert (
        rebar.llm.review_plan(plan_b, runner=_PassRunner(), repo_root=str(rebar_repo), force=True)[
            "verdict"
        ]
        == "PASS"
    )
    assert (
        rebar.llm.review_plan(plan_c, runner=_PassRunner(), repo_root=str(rebar_repo), force=True)[
            "verdict"
        ]
        == "PASS"
    )

    rebar.edit_ticket(
        plan_a,
        description=_DESCRIPTION + "\nA changed narrowly.",
        repo_root=str(rebar_repo),
    )

    direct = _cli(rebar_repo, "claim", plan_b, "--assignee=fixture")
    before_propagation = _cli(rebar_repo, "review-plan", plan_c, "--status")
    assert direct.returncode == 1
    assert "stale-pin-drift" in direct.stderr
    assert rebar.show_ticket(plan_b, repo_root=str(rebar_repo))["status"] == "open"
    assert before_propagation.returncode == 0, before_propagation.stderr

    unchanged = rebar.llm.review_plan(
        plan_b, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
    )
    after_unchanged_review = _cli(rebar_repo, "review-plan", plan_c, "--status")

    assert unchanged["verdict"] == "PASS"
    assert after_unchanged_review.returncode == 0, after_unchanged_review.stderr

    rebar.edit_ticket(
        plan_b,
        description=_DESCRIPTION + "\nB adapted to A.",
        repo_root=str(rebar_repo),
    )
    propagated = _cli(rebar_repo, "review-plan", plan_c, "--status")

    assert propagated.returncode == 12
    assert "stale-pin-drift" in propagated.stdout + propagated.stderr
    assert rebar.show_ticket(plan_c, repo_root=str(rebar_repo))["status"] == "open"


def test_depends_on_and_inbound_blocks_normalize_to_one_canonical_prerequisite_pin(
    rebar_repo: Path,
) -> None:
    _assert_project_policies_enabled()
    _commit(rebar_repo)
    _enable_fixture(rebar_repo)
    prerequisite = _ticket(rebar_repo, "canonical dual-direction prerequisite")
    depends_subject = _ticket(rebar_repo, "outgoing depends-on subject")
    blocked_subject = _ticket(rebar_repo, "inbound blocks subject")
    rebar.link(depends_subject, prerequisite, "depends_on", repo_root=str(rebar_repo))
    rebar.link(prerequisite, blocked_subject, "blocks", repo_root=str(rebar_repo))

    # Both subjects are link-blocked by the open prerequisite (one via depends_on, one
    # via an inbound blocks); force past the not-claimable gate to compare their pins.
    depends_result = rebar.llm.review_plan(
        depends_subject, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
    )
    blocks_result = rebar.llm.review_plan(
        blocked_subject, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
    )
    prefix = f"plan-material-pin: prerequisite {prerequisite} "
    matching_pins = []
    for subject in (depends_subject, blocked_subject):
        signature = rebar.verify_signature(subject, kind="plan-review", repo_root=str(rebar_repo))
        matching_pins.append([line for line in signature["manifest"] if line.startswith(prefix)])

    assert depends_result["verdict"] == blocks_result["verdict"] == "PASS"
    assert all(len(pins) == 1 for pins in matching_pins)
    assert matching_pins[0] == matching_pins[1]
    assert all(
        _cli(rebar_repo, "review-plan", subject, "--status").returncode == 0
        for subject in (depends_subject, blocked_subject)
    )


def test_in_progress_to_open_resets_phase_and_requires_a_planning_review(
    rebar_repo: Path,
) -> None:
    _assert_project_policies_enabled()
    _commit(rebar_repo)
    _enable_fixture(rebar_repo)
    ticket_id = _ticket(rebar_repo, "return to open resets review")
    assert (
        rebar.llm.review_plan(ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo))["verdict"]
        == "PASS"
    )
    assert _cli(rebar_repo, "claim", ticket_id, "--assignee=fixture").returncode == 0
    assert (
        rebar.llm.review_plan(
            ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
        )["verdict"]
        == "PASS"
    )
    returned = _cli(rebar_repo, "transition", ticket_id, "in_progress", "open")
    assert returned.returncode == 0, returned.stderr

    state = rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))
    assert state["status"] == "open"
    assert state["plan_review_phase"] == "planning"
    status = _cli(rebar_repo, "review-plan", ticket_id, "--status")
    rejected = _cli(rebar_repo, "claim", ticket_id, "--assignee=fixture")
    assert status.returncode == 12
    assert "incompatible-phase" in status.stdout + status.stderr
    assert rejected.returncode == 1
    assert "phase is incompatible" in rejected.stderr

    assert (
        rebar.llm.review_plan(
            ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
        )["verdict"]
        == "PASS"
    )
    assert _cli(rebar_repo, "claim", ticket_id, "--assignee=fixture").returncode == 0
    claimed = rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))
    assert claimed["status"] == "in_progress"
    assert claimed["plan_review_phase"] == "execution"


def test_close_allows_code_only_head_drift_after_execution_review(
    rebar_repo: Path,
) -> None:
    _assert_project_policies_enabled()
    _commit(rebar_repo)
    _enable_fixture(rebar_repo)
    ticket_id = _ticket(rebar_repo, "code-only drift at close")
    assert (
        rebar.llm.review_plan(ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo))["verdict"]
        == "PASS"
    )
    assert _cli(rebar_repo, "claim", ticket_id, "--assignee=fixture").returncode == 0
    assert (
        rebar.llm.review_plan(
            ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
        )["verdict"]
        == "PASS"
    )
    signing.sign_manifest(
        ticket_id,
        ["completion-verifier: PASS", f"ticket: {ticket_id}"],
        kind="completion-verifier",
        repo_root=str(rebar_repo),
    )
    _commit(rebar_repo)

    result = _cli(rebar_repo, "transition", ticket_id, "in_progress", "closed")

    assert result.returncode == 0, result.stderr
    assert rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))["status"] == "closed"


def test_in_progress_self_edit_requires_fresh_execution_review_before_close(
    rebar_repo: Path,
) -> None:
    _assert_project_policies_enabled()
    _commit(rebar_repo)
    _enable_fixture(rebar_repo)
    ticket_id = _ticket(rebar_repo, "execution self-edit")
    assert (
        rebar.llm.review_plan(ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo))["verdict"]
        == "PASS"
    )
    assert _cli(rebar_repo, "claim", ticket_id, "--assignee=fixture").returncode == 0
    assert (
        rebar.llm.review_plan(
            ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
        )["verdict"]
        == "PASS"
    )

    rebar.edit_ticket(
        ticket_id,
        description=_DESCRIPTION + "\nExecution adaptation.",
        repo_root=str(rebar_repo),
    )
    rejected = _cli(rebar_repo, "transition", ticket_id, "in_progress", "closed")

    assert rejected.returncode == 1
    assert "stale-material" in rejected.stderr
    assert "review-plan" in rejected.stderr
    assert rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))["status"] == "in_progress"

    assert (
        rebar.llm.review_plan(
            ticket_id, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
        )["verdict"]
        == "PASS"
    )
    closed = _cli(rebar_repo, "transition", ticket_id, "in_progress", "closed")

    assert closed.returncode == 0, closed.stderr
    assert rebar.show_ticket(ticket_id, repo_root=str(rebar_repo))["status"] == "closed"


def test_material_pin_enforcement_enable_disable_and_reenable_through_real_cli(
    rebar_repo: Path,
) -> None:
    _assert_project_policies_enabled()
    _commit(rebar_repo)
    _enable_fixture(rebar_repo)
    enabled_prerequisite = _ticket(rebar_repo, "enabled prerequisite")
    enabled_subject = _ticket(rebar_repo, "enabled subject")
    rebar.link(enabled_subject, enabled_prerequisite, "depends_on", repo_root=str(rebar_repo))
    # Link-blocked by an open prerequisite; force past the not-claimable gate.
    assert (
        rebar.llm.review_plan(
            enabled_subject, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
        )["verdict"]
        == "PASS"
    )
    rebar.edit_ticket(
        enabled_prerequisite,
        description=_DESCRIPTION + "\nEnabled drift.",
        repo_root=str(rebar_repo),
    )
    enabled = _cli(rebar_repo, "claim", enabled_subject, "--assignee=fixture")
    assert enabled.returncode == 1
    assert rebar.show_ticket(enabled_subject, repo_root=str(rebar_repo))["status"] == "open"

    _set_fixture_pin_enforcement(rebar_repo, enabled=False)
    disabled = _cli(rebar_repo, "claim", enabled_subject, "--assignee=fixture")
    assert disabled.returncode == 0, disabled.stderr

    reenabled_prerequisite = _ticket(rebar_repo, "re-enabled prerequisite")
    reenabled_subject = _ticket(rebar_repo, "re-enabled subject")
    rebar.link(reenabled_subject, reenabled_prerequisite, "depends_on", repo_root=str(rebar_repo))
    # Link-blocked by an open prerequisite; force past the not-claimable gate.
    assert (
        rebar.llm.review_plan(
            reenabled_subject, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
        )["verdict"]
        == "PASS"
    )
    rebar.edit_ticket(
        reenabled_prerequisite,
        description=_DESCRIPTION + "\nDisabled drift.",
        repo_root=str(rebar_repo),
    )
    _set_fixture_pin_enforcement(rebar_repo, enabled=True)
    reenabled = _cli(rebar_repo, "claim", reenabled_subject, "--assignee=fixture")

    assert reenabled.returncode == 1
    assert "stale-pin-drift" in reenabled.stderr
    assert rebar.show_ticket(reenabled_subject, repo_root=str(rebar_repo))["status"] == "open"


def test_archived_target_stays_readable_and_deleted_target_fails_safe(
    rebar_repo: Path,
) -> None:
    _assert_project_policies_enabled()
    _commit(rebar_repo)
    _enable_fixture(rebar_repo)
    archived_target = _ticket(rebar_repo, "archived readable prerequisite")
    archived_subject = _ticket(rebar_repo, "subject with archived prerequisite")
    rebar.link(archived_subject, archived_target, "depends_on", repo_root=str(rebar_repo))
    # Prerequisite is still open at review time, so the subject is link-blocked; force
    # past the not-claimable gate to pin it before it is archived below.
    assert (
        rebar.llm.review_plan(
            archived_subject, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
        )["verdict"]
        == "PASS"
    )
    rebar.archive(archived_target, repo_root=str(rebar_repo))
    archived = _cli(rebar_repo, "claim", archived_subject, "--assignee=fixture")

    assert archived.returncode == 0, archived.stderr
    assert rebar.show_ticket(archived_subject, repo_root=str(rebar_repo))["status"] == "in_progress"

    deleted_target = _ticket(rebar_repo, "deleted prerequisite")
    deleted_subject = _ticket(rebar_repo, "subject with deleted prerequisite")
    rebar.link(deleted_subject, deleted_target, "depends_on", repo_root=str(rebar_repo))
    # Prerequisite is still open at review time, so the subject is link-blocked; force
    # past the not-claimable gate to pin it before it is deleted below.
    assert (
        rebar.llm.review_plan(
            deleted_subject, runner=_PassRunner(), repo_root=str(rebar_repo), force=True
        )["verdict"]
        == "PASS"
    )
    deleted = _cli(rebar_repo, "delete", deleted_target, "--user-approved")
    rejected = _cli(rebar_repo, "claim", deleted_subject, "--assignee=fixture")

    assert deleted.returncode == 0, deleted.stderr
    assert rejected.returncode == 1
    assert "stale-pin-missing" in rejected.stderr
    assert deleted_target in rejected.stderr
    assert rebar.show_ticket(deleted_subject, repo_root=str(rebar_repo))["status"] == "open"
