"""Deterministic pre-gates for the four zero-finding AGENT criteria (ticket 4ee2).

T13 / T14 / removal-rationale / asserted-capability produced ZERO findings over ~7,900
agentic runs, so each now has a conservative deterministic PRE-FILTER in front of the
unchanged LLM router (ADR 0034 amendment): a :class:`DetGateRule` that FIRES when
(all ``text_all`` regexes match the plan text) OR (any ``file_impact_globs`` fnmatch
matches a declared file_impact path) OR (``file_impact_reason_re`` matches an entry's
reason or path). A fired plan routes to the LLM exactly as before; a not-fired plan
skips the criterion with zero LLM routing, recorded in ``gate_log`` (surfaced in the
sidecar as ``coverage.routing.det_gated``).

These tests pin: the trigger vocabularies fire (audit evasion classes), non-matching
plans skip with a det_gated record and zero LLM routing, the asserted-capability
conjunction (both legs required), the file_impact glob/reason arms firing on
file_impact alone, T5a/T5d/T7/T12 behavior preservation across the DetGateRule
migration, and the ADR 0034 amendment note.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rebar.llm.plan_review import orchestrator, registry
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.registry import (
    _DET_LEAF_GATE_RULES,
    _DET_OVERLAY_RULES,
    DetGateRule,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[3]

# A plan whose vocabulary is entirely absent from ALL FOUR trigger sets (and the
# T5a/T5d/T7/T12 vocabularies): the total-vocabulary-absence skip case.
_NEUTRAL = (
    "Improve the wording of two user-facing error strings.\n\n"
    "## Acceptance Criteria\n- [ ] both strings read clearly\n"
)


def _ctx(description: str, *, ttype: str = "task", state: dict | None = None) -> PlanContext:
    return PlanContext(
        ticket_id="abcd-0000-0000-0002",
        ticket_type=ttype,
        title="T",
        description=description,
        state=state or {},
    )


def _routed_ids(description: str, *, state: dict | None = None, gate_log=None) -> set[str]:
    single, agent = orchestrator.route_criteria(_ctx(description, state=state), gate_log=gate_log)
    return {c["id"] for c in single + agent}


# ── audit evasion classes fire their triggers ─────────────────────────────────
@pytest.mark.parametrize(
    "sentence",
    [
        # mechanism-of-enforcement vocabulary (the T13 audit's evasion classes)
        "The gate refuses any claim that lacks a current attestation.",
        "CI fails the build when a module exceeds the cap.",
        "The helper exits non-zero on a stale pin.",
        "Closing is disallowed until the change merges.",
        "The pre-commit hook bounces commits missing the trailer.",
        "Claiming raises ConcurrencyError when another agent holds the ticket.",
        "You cannot merge without both votes.",
        "Landing now requires a signed attestation.",
        "A failing probe turns the build red.",
        # additive-obligation lexicon (story 5bca-4ca9 P2: T13 widened to obligations)
        "Every adapter must expose the new parameter.",
        "Callers must pass the new argument explicitly.",
        "The exporter adds a required class argument.",
        "The MCP schema must stay in sync with the CLI grammar.",
        "This introduces a new invariant on ticket identifiers.",
    ],
)
def test_t13_trigger_fires(sentence: str) -> None:
    assert _DET_OVERLAY_RULES["T13"].fires(sentence) is True


@pytest.mark.parametrize(
    "sentence",
    [
        # scheduler / release / CI-surface vocabulary (the T14 audit's evasion classes)
        "Add a nightly cron sync for the mirror.",
        "Extend .github/workflows to cover the audit.",
        "The release pipeline gains a step that uploads to PyPI.",
        "A new CI job checks the wheel metadata.",
        "Register a console_scripts entry point for the tool.",
        "Pushes to refs/for/main create a review.",
        "The LaunchAgent runs hourly on this host.",
        "Wire the check into the Makefile.",
    ],
)
def test_t14_trigger_fires(sentence: str) -> None:
    assert _DET_OVERLAY_RULES["T14"].fires(sentence) is True


@pytest.mark.parametrize(
    "sentence",
    [
        # removal / weakening vocabulary (the removal-rationale audit's evasion classes)
        "Delete the legacy shim module.",
        "This retires the fallback parser.",
        "The alert is silenced in quiet mode.",
        "Fold the two helpers into one.",
        "The hard failure becomes a warning.",
        "Bypass the cache when the flag is set.",
        "The old path is no longer exercised.",
        "Rip out the transitional adapter.",
        "Downgrade the finding to advisory.",
        "Make the second retry a no-op.",
    ],
)
def test_removal_rationale_trigger_fires(sentence: str) -> None:
    assert _DET_LEAF_GATE_RULES["removal-rationale"].fires(sentence) is True


@pytest.mark.parametrize(
    "sentence",
    [
        # module-ref leg AND capability-verb leg together (the conjunction)
        "The registry module already exposes a loader for this.",
        "workflow_ops.py lacks a coverage merge step, so it must be built.",
        "sizing.py already handles the overlay shed order.",
        "The verifier delegates to the shared review kernel helper.",
        "rebar.llm.runner provides the retry loop we lean on.",
        "There is no existing sidecar writer for this payload.",
    ],
)
def test_asserted_capability_trigger_fires(sentence: str) -> None:
    assert _DET_LEAF_GATE_RULES["asserted-capability"].fires(sentence) is True


# ── asserted-capability is a CONJUNCTION: one leg alone never fires ───────────
def test_asserted_capability_module_leg_alone_does_not_fire() -> None:
    # module-ref leg matches ("module"); capability-verb leg has no match.
    assert _DET_LEAF_GATE_RULES["asserted-capability"].fires("Tidy one module docstring.") is False


def test_asserted_capability_verb_leg_alone_does_not_fire() -> None:
    # capability-verb leg matches ("already"); module-ref leg has no match.
    assert _DET_LEAF_GATE_RULES["asserted-capability"].fires("It already reads well.") is False


# ── file_impact arms fire on file_impact ALONE (non-matching text) ────────────
def test_t14_glob_arm_fires_on_file_impact_alone() -> None:
    rule = _DET_OVERLAY_RULES["T14"]
    fi = [{"path": ".github/workflows/x.yml", "reason": "adjust matrix"}]
    assert rule.fires(_NEUTRAL, file_impact=fi) is True
    assert rule.fires(_NEUTRAL, file_impact=[{"path": "src/rebar/x.py", "reason": "edit"}]) is False


def test_removal_reason_arm_fires_on_file_impact_alone() -> None:
    rule = _DET_LEAF_GATE_RULES["removal-rationale"]
    fi = [{"path": "src/rebar/shim.py", "reason": "delete the legacy shim"}]
    assert rule.fires(_NEUTRAL, file_impact=fi) is True
    # The reason regex also runs over the PATH string (the only other removal signal).
    fi_path = [{"path": "src/rebar/removal_helpers.py", "reason": "touch"}]
    assert rule.fires(_NEUTRAL, file_impact=fi_path) is True
    assert rule.fires(_NEUTRAL, file_impact=[{"path": "src/rebar/x.py", "reason": "edit"}]) is False


# ── route_criteria: non-matching plans SKIP with det_gated coverage ───────────
def test_route_criteria_skips_all_four_on_neutral_plan_and_logs() -> None:
    gate_log: dict[str, str] = {}
    routed = _routed_ids(_NEUTRAL, gate_log=gate_log)
    for cid in ("T13", "T14", "removal-rationale", "asserted-capability"):
        assert cid not in routed, f"{cid} must not route on a vocabulary-absent plan"
        assert gate_log[cid], f"{cid} skip must be recorded in gate_log"
    # Overlay skips go through the overlay guard, leaf skips through the new leaf block —
    # both record the RULE NAME of the not-fired gate.
    assert gate_log["T13"] == _DET_OVERLAY_RULES["T13"].name
    assert gate_log["T14"] == _DET_OVERLAY_RULES["T14"].name
    assert gate_log["removal-rationale"] == _DET_LEAF_GATE_RULES["removal-rationale"].name
    assert gate_log["asserted-capability"] == _DET_LEAF_GATE_RULES["asserted-capability"].name


def test_route_criteria_fired_plans_route_unchanged() -> None:
    plan = (
        "## Approach\n"
        "The gate refuses a claim without attestation (CI fails the build otherwise).\n"
        "Add a nightly cron sync and a new CI job for the wheel.\n"
        "Delete the legacy shim module; the sizing module already handles the shed order.\n"
        "## Acceptance Criteria\n- [ ] done\n"
    )
    gate_log: dict[str, str] = {}
    routed = _routed_ids(plan, gate_log=gate_log)
    for cid in ("T13", "T14", "removal-rationale", "asserted-capability"):
        assert cid in routed, f"{cid} must route when its trigger fires"
        assert cid not in gate_log


def test_route_criteria_file_impact_arms_reach_the_gates() -> None:
    state = {
        "file_impact": [
            {"path": ".github/workflows/ci.yml", "reason": "adjust matrix"},
            {"path": "src/rebar/shim.py", "reason": "delete the legacy shim"},
        ]
    }
    routed = _routed_ids(_NEUTRAL, state=state)
    assert "T14" in routed  # glob arm, text entirely non-matching
    assert "removal-rationale" in routed  # reason arm, text entirely non-matching


def test_route_criteria_gate_log_default_is_optional() -> None:
    # The parameter is optional: existing callers without a gate_log still work.
    single, agent = orchestrator.route_criteria(_ctx(_NEUTRAL))
    assert "T13" not in {c["id"] for c in single + agent}


# ── the assemble step surfaces gate_log as routing.det_gated (sidecar payload) ─
def test_assemble_step_emits_det_gated_coverage(monkeypatch) -> None:
    from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the ops
    from rebar.llm.workflow.executor import STEP_REGISTRY, StepContext

    state = {
        "ticket_id": "T-1",
        "ticket_type": "task",
        "title": "T",
        "description": _NEUTRAL,
        "deps": [],
    }
    monkeypatch.setattr("rebar._reads.show_ticket", lambda tid, repo_root=None: dict(state))
    monkeypatch.setattr("rebar._reads.list_tickets", lambda parent=None, repo_root=None: [])
    op = STEP_REGISTRY["plan_review_assemble_criteria"]
    ctx = StepContext(
        run_id="r",
        step_id="assemble",
        kind="scripted",
        step={},
        inputs={"ticket_id": "T-1"},
        workflow={},
        target_ticket="T-1",
        repo_root=None,
    )
    out = op(ctx)
    det_gated = out["routing"]["det_gated"]
    for cid in ("T13", "T14", "removal-rationale", "asserted-capability"):
        assert cid in det_gated
        assert out.get(f"include_{cid}") is False
    # A criterion the gates did not skip carries no det_gated entry.
    assert "E1" not in det_gated


# ── T5a/T5d/T7/T12: the DetGateRule migration is behavior-preserving ──────────
_LEGACY_RULES = {
    "T5a": r"\b(latency|throughput|performance|scal\w*|n\+1|batch|cache|memory|hot[- ]?path)\b",
    "T5d": r"\b(ui|button|form|screen|page|modal|wcag|aria|accessib\w*|keyboard|contrast)\b",
    "T7": r"\b(\bdocs?\b|readme|claude\.md|adr|guide|documentation)\b",
    "T12": r"\b(deploy|rollout|canary|feature flag|production traffic|rollback|blue.green)\b",
}
_REPRESENTATIVE_TEXTS = [
    _NEUTRAL,
    "This plan changes performance and latency on the hot path.",
    "Add a cache and cut the p99 latency.",
    "The modal form gets a keyboard-accessible button.",
    "Update the README and the ADR, then regenerate the docs guide.",
    "Roll out behind a feature flag with a canary deploy and a rollback plan.",
    "Rename a helper and fix a typo.",
]


@pytest.mark.parametrize("cid", sorted(_LEGACY_RULES))
@pytest.mark.parametrize("text", _REPRESENTATIVE_TEXTS)
def test_migrated_rules_preserve_behavior(cid: str, text: str) -> None:
    rule = _DET_OVERLAY_RULES[cid]
    assert isinstance(rule, DetGateRule)
    assert rule.fires(text) is bool(re.search(_LEGACY_RULES[cid], text, re.IGNORECASE))


def test_overlay_triggers_map_covers_det_table() -> None:
    fired = registry.overlay_triggers("This plan changes performance and latency on the hot path.")
    assert fired["T5a"] is True
    assert set(fired) == set(_DET_OVERLAY_RULES)


# ── ADR 0034 carries the amendment note ───────────────────────────────────────
def test_adr_0034_amendment_note() -> None:
    text = (_ROOT / "docs/adr/0034-llm-routed-enumeration-overlays.md").read_text(encoding="utf-8")
    assert "Amendment" in text
    assert "pre-filter" in text.lower()
    assert "zero findings" in text.lower()
    assert "1,939" in text
