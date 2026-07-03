"""WS1 (epic b744): the OVERLAY_IDS enum, the recommend_overlays filter (drop-not-error),
the base reviewer output contract (mode=structured preserves BOTH named outputs), the
base-step failure fallback, and the prompt<->enum no-drift guard.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from rebar import schemas
from rebar.llm.code_review import registry as reg

pytestmark = pytest.mark.unit


# ── the enum + its derived sources ────────────────────────────────────────────────────────
def test_overlay_ids_is_the_expected_closed_set():
    assert set(reg.OVERLAY_IDS) == {
        "security",
        "performance",
        "i18n",
        "a11y",
        "db-migrations",
        "docs",
        "supply-chain",
        "api-compat",
        "iac",
        "tests",
        "llm-prompts",
    }
    # closed + ordered: enum() is derived from the one constant (no second source to drift)
    assert reg.overlay_id_enum() == list(reg.OVERLAY_IDS)


def test_is_overlay_id():
    assert reg.is_overlay_id("security")
    assert not reg.is_overlay_id("made-up")
    assert not reg.is_overlay_id(None)
    assert not reg.is_overlay_id(123)


# ── filter: out-of-enum DROPPED (not errored), reason required + truncated, dedup ──────────
def test_filter_drops_out_of_enum_ids_without_erroring():
    raw = [
        {"overlay_id": "security", "reason": "touches auth"},
        {"overlay_id": "made-up", "reason": "should be dropped"},
        {"overlay_id": "tests", "reason": "no new tests"},
    ]
    out = reg.filter_recommend_overlays(raw)
    assert [o["overlay_id"] for o in out] == ["security", "tests"]


def test_filter_drops_entries_missing_or_blank_reason():
    raw = [
        {"overlay_id": "security"},  # no reason
        {"overlay_id": "tests", "reason": "   "},  # blank reason
        {"overlay_id": "docs", "reason": "docs must track"},
    ]
    assert reg.recommend_overlay_ids(raw) == ["docs"]


def test_filter_truncates_overlong_reason():
    out = reg.filter_recommend_overlays([{"overlay_id": "iac", "reason": "x" * 500}])
    assert len(out) == 1
    assert len(out[0]["reason"]) == reg.REASON_MAX_CHARS


def test_filter_dedups_by_overlay_id_first_wins():
    raw = [
        {"overlay_id": "security", "reason": "first"},
        {"overlay_id": "security", "reason": "second"},
    ]
    out = reg.filter_recommend_overlays(raw)
    assert len(out) == 1 and out[0]["reason"] == "first"


def test_filter_failsoft_on_malformed_input():
    assert reg.filter_recommend_overlays(None) == []
    assert reg.filter_recommend_overlays("nope") == []
    assert reg.filter_recommend_overlays([1, "x", {"no": "id"}]) == []


# ── base-step failure fallback: empty findings + coverage-gap, never a BLOCK ───────────────
def test_base_failure_result_is_empty_findings_plus_coverage_gap():
    res = reg.base_failure_result("timeout")
    assert res["findings"] == []
    assert res["recommend_overlays"] == []
    gaps = res["coverage_gaps"]
    assert len(gaps) == 1
    assert gaps[0]["dimension"] == reg.COVERAGE_GAP_DIMENSION
    assert gaps[0]["reviewer_id"] == reg.BASE_REVIEWER_ID
    assert "timeout" in gaps[0]["detail"]
    # the fallback carries NO blocking signal of any kind (recall-side, never the verdict)
    assert "verdict" not in res and "block" not in res


# ── the output contract: mode=structured preserves BOTH named outputs ──────────────────────
def test_base_output_schema_accepts_both_keys():
    v = schemas.validator("code_review_base_output")
    v.validate(
        {
            "findings": [
                {"finding": "off-by-one", "criteria": ["correctness"], "evidence": ["x.py:3"]}
            ],
            "recommend_overlays": [{"overlay_id": "security", "reason": "auth path"}],
        }
    )
    # findings is required; recommend_overlays optional
    v.validate({"findings": []})


def test_structured_runner_preserves_recommend_overlays_but_findings_mode_strips_it():
    """The load-bearing wiring: the base step MUST run with mode='structured' +
    output_schema='code_review_base_output' so recommend_overlays survives. The default
    mode='findings' path (finalize_findings) keeps ONLY findings — proving why the
    standalone-structured step is required (not a batch finder)."""
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import FakeRunner, RunRequest

    cfg = LLMConfig.from_env()
    payload = {
        "findings": [{"finding": "bug", "criteria": ["correctness"], "evidence": ["a.py:1"]}],
        "recommend_overlays": [{"overlay_id": "tests", "reason": "no regression test"}],
        "summary": "1 finding",
    }
    structured = FakeRunner(structured=payload).run(
        RunRequest(
            system_prompt="",
            instructions="",
            config=cfg,
            mode="structured",
            output_schema="code_review_base_output",
        )
    )
    assert structured["findings"] and structured["recommend_overlays"]
    assert structured["recommend_overlays"][0]["overlay_id"] == "tests"

    findings_mode = FakeRunner(findings=payload["findings"]).run(
        RunRequest(system_prompt="", instructions="", config=cfg, mode="findings")
    )
    # the findings-mode envelope carries findings but NOT the recommend_overlays signal
    assert "recommend_overlays" not in findings_mode


# ── no-drift guard: the prompt body enumerates EXACTLY the OVERLAY_IDS ──────────────────────
def test_base_prompt_enumerates_exactly_the_overlay_ids():
    body = pathlib.Path("src/rebar/llm/reviewers/code-review-base.md").read_text()
    # the catalog is rendered as a `- \`<id>\` —` bullet per overlay
    listed = set(re.findall(r"^- `([a-z0-9-]+)` —", body, flags=re.MULTILINE))
    assert listed == set(reg.OVERLAY_IDS), (
        "code-review-base.md overlay catalog drifted from OVERLAY_IDS: "
        f"prompt-only={listed - set(reg.OVERLAY_IDS)}, enum-only={set(reg.OVERLAY_IDS) - listed}"
    )


def test_base_prompt_declares_the_structured_output_contract():
    from rebar.llm.prompting.prompts import get_prompt

    p = get_prompt("code-review-base")
    assert p.outputs == "code_review_base_output"
    assert p.execution_mode == "agentic"  # tool-using; reads the changed files
    assert not p.is_reviewer  # stays OUT of the single-pass reviewer-selection catalog


def test_registered_response_model_carries_recommend_overlays():
    """C1: the LIVE runner builds its structured-output model via
    contracts.response_model_for(output_schema) — keyed off the CONTRACTS registry, NOT the
    JSON-Schema registry. If no Pydantic contract is registered for code_review_base_output,
    the runner silently falls back to the default findings+summary model and the model can
    NEVER emit recommend_overlays. This pins that the contract IS registered and carries both
    named outputs. (Importing `reg` above triggers the package __init__ -> contract register.)"""
    from rebar.llm import contracts
    from rebar.llm.code_review import registry  # noqa: F401 — ensure the package __init__ ran

    model = contracts.response_model_for("code_review_base_output")
    fields = set(model.model_fields)
    assert "recommend_overlays" in fields, (
        "the registered contract must carry recommend_overlays, else the real runner drops it"
    )
    assert "findings" in fields
    # it must NOT be the default findings model (which lacks recommend_overlays)
    default = contracts.response_model_for(None)
    assert "recommend_overlays" not in set(default.model_fields)

    # a model instance round-trips both named outputs through pydantic validation
    inst = model(
        findings=[{"finding": "bug", "criteria": ["correctness"], "evidence": ["a.py:1"]}],
        recommend_overlays=[{"overlay_id": "security", "reason": "auth path"}],
    )
    dumped = inst.model_dump()
    assert dumped["recommend_overlays"][0]["overlay_id"] == "security"
    # evidence stays a LIST (kernel Pass-2 joins it — a bare string would crash that)
    assert isinstance(dumped["findings"][0]["evidence"], list)
