"""Held-out oracle for the insufficient-evidence sibling marker (ticket 1d71-a76c-04f6-4642).

When the bounded evidence search exhausts without finding evidence, the framework used to
bank a bare ``met=false`` — indistinguishable from a positive refutation. These tests pin
the fix: a framework-set ``evidence_sufficient: false`` sibling marker (modeled on the
``verdict_obtainable`` precedent) that rides on bank entries, criteria records, and the
top-level verdict, changing ONLY message rendering and the recorded evidence class. The
verdict vocabulary stays {PASS, FAIL}; ``met`` stays bool; every fail-closed consumer still
blocks. Offline only — no live LLM.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from rebar.llm.workflow import completion_banking as cb
from rebar.llm.workflow import completion_criteria as ccrit
from rebar.llm.workflow import completion_recovery as cr

pytestmark = pytest.mark.unit


def _bank(tmp_path, ticket="T-1", material=None, tree=None):
    stamps = cb.BankStamps(ticket_id=ticket, material_fingerprint=material, tree_sha=tree)
    return cb.CriterionBank(tmp_path / "bank", stamps)


# ── bank write path: the bounded fallback owns insufficiency ─────────────────────────
def test_bounded_fallback_banks_insufficient_marker(tmp_path) -> None:
    """``make_completion_record_tool``'s fallback banks met=false PLUS the marker."""
    from rebar.llm.completion_tool_policy import (
        BOUNDED_FALLBACK_EVIDENCE,
        COMPLETION_EVIDENCE_POLICY_ATTR,
        make_completion_record_tool,
    )

    bank = _bank(tmp_path)
    tool = make_completion_record_tool(bank, ("c00-aaaa",))
    policy = getattr(tool, COMPLETION_EVIDENCE_POLICY_ATTR)
    policy.fallback_record("c00-aaaa", BOUNDED_FALLBACK_EVIDENCE)
    entry = bank.get("c00-aaaa")
    assert entry is not None
    assert entry["met"] is False
    assert entry["evidence_sufficient"] is False


def test_model_record_tool_stays_bool_only_and_banks_bare_met_false(tmp_path) -> None:
    """The model-facing tool signature is unchanged and a tool record carries NO marker —
    a model met=false is a positive refutation, not insufficiency."""
    bank = _bank(tmp_path)
    tool = bank.make_record_tool()
    params = list(inspect.signature(tool).parameters)
    assert params == ["criterion_id", "met", "evidence"]
    tool("c00-aaaa", False, "the file does not exist")
    entry = bank.get("c00-aaaa")
    assert entry["met"] is False
    assert "evidence_sufficient" not in entry


def test_record_insufficient_caps_evidence_and_persists_marker(tmp_path) -> None:
    bank = _bank(tmp_path)
    entry = bank.record_insufficient("c00-aaaa", "x" * 4000)
    assert entry["truncated"] is True
    reread = bank.get("c00-aaaa")
    assert reread["evidence_sufficient"] is False and reread["met"] is False


def test_bounded_fallback_texts_say_insufficient_not_met_false() -> None:
    """The fallback notice/evidence describe insufficiency, not a recorded refutation."""
    from rebar.llm.completion_tool_policy import (
        BOUNDED_FALLBACK_EVIDENCE,
        BOUNDED_FALLBACK_NOTICE,
    )

    for text in (BOUNDED_FALLBACK_EVIDENCE, BOUNDED_FALLBACK_NOTICE):
        lowered = text.casefold()
        assert "insufficient" in lowered
        assert "met=false" not in lowered


def test_record_tool_instruction_is_refutation_only(tmp_path) -> None:
    """The tool docstring instructs met=false ONLY on positive refutation; not-found
    records nothing (the bounded fallback owns insufficiency)."""
    doc = (inspect.getdoc(_bank(tmp_path).make_record_tool()) or "").casefold()
    assert "refut" in doc
    assert "record nothing" in doc or "do not record" in doc


# ── harvest: preserve-on-overwrite ───────────────────────────────────────────────────
def test_harvest_preserves_marker_on_markerless_met_false_overwrite(tmp_path) -> None:
    """Model structured output never carries the marker; a successor echoing a banked
    insufficiency as met=false must not silently upgrade it to a refutation."""
    bank = _bank(tmp_path)
    bank.record_insufficient("c00-aaaa", "bounded search exhausted")
    result = {"criteria": [{"criterion_id": "c00-aaaa", "met": False, "evidence": "echo"}]}
    assert cb.harvest_structured_into_bank(bank, result, {}) == 1
    entry = bank.get("c00-aaaa")
    assert entry["met"] is False and entry["evidence_sufficient"] is False


def test_harvest_met_true_clears_marker(tmp_path) -> None:
    bank = _bank(tmp_path)
    bank.record_insufficient("c00-aaaa", "bounded search exhausted")
    result = {"criteria": [{"criterion_id": "c00-aaaa", "met": True, "evidence": "found"}]}
    cb.harvest_structured_into_bank(bank, result, {})
    assert "evidence_sufficient" not in bank.get("c00-aaaa")


def test_tool_refutation_replaces_marker(tmp_path) -> None:
    """A genuine tool-recorded refutation REPLACES a prior insufficiency marker."""
    bank = _bank(tmp_path)
    bank.record_insufficient("c00-aaaa", "bounded search exhausted")
    bank.make_record_tool()("c00-aaaa", False, "positively refuted")
    entry = bank.get("c00-aaaa")
    assert entry["met"] is False and "evidence_sufficient" not in entry


def test_harvest_ignores_model_supplied_marker(tmp_path) -> None:
    """The marker is framework-owned: a model minting it in structured output is dropped."""
    bank = _bank(tmp_path)
    result = {
        "criteria": [
            {
                "criterion_id": "c00-aaaa",
                "met": False,
                "evidence": "e",
                "evidence_sufficient": False,
            }
        ]
    }
    cb.harvest_structured_into_bank(bank, result, {})
    assert "evidence_sufficient" not in bank.get("c00-aaaa")


# ── deterministic assembly ───────────────────────────────────────────────────────────
def test_deterministic_verdict_marks_insufficient_banked_entries(tmp_path) -> None:
    bank = _bank(tmp_path)
    bank.record_insufficient("c00-aaaa", "bounded search exhausted")
    ids = {"Ship the fix": "c00-aaaa"}
    verdict = cb.assemble_deterministic_verdict("T-1", ["Ship the fix"], bank.all(), id_by_text=ids)
    assert verdict["verdict"] == "FAIL"
    (record,) = verdict["criteria"]
    assert record["met"] is False and record["evidence_sufficient"] is False
    (finding,) = verdict["findings"]
    assert "insufficient" in finding["detail"].casefold()
    assert "not met." not in finding["detail"].casefold()


def test_deterministic_verdict_unbanked_placeholder_carries_marker() -> None:
    ids = {"Ship the fix": "c00-aaaa"}
    verdict = cb.assemble_deterministic_verdict("T-1", ["Ship the fix"], {}, id_by_text=ids)
    (record,) = verdict["criteria"]
    assert record["met"] is False
    assert record["evidence_sufficient"] is False
    assert record["unverified"] is True and record["exhausted"] is True
    (finding,) = verdict["findings"]
    assert "insufficient" in finding["detail"].casefold()


def test_deterministic_genuine_refutation_stays_bare_unmet(tmp_path) -> None:
    bank = _bank(tmp_path)
    bank.make_record_tool()("c00-aaaa", False, "refuted")
    ids = {"Ship the fix": "c00-aaaa"}
    verdict = cb.assemble_deterministic_verdict("T-1", ["Ship the fix"], bank.all(), id_by_text=ids)
    (record,) = verdict["criteria"]
    assert record["met"] is False and "evidence_sufficient" not in record
    (finding,) = verdict["findings"]
    assert "insufficient" not in finding["detail"].casefold()


# ── finalizer merge: the bank is the authoritative re-stamp ──────────────────────────
def _marked_entries(tmp_path) -> dict:
    bank = _bank(tmp_path)
    bank.record_insufficient("c00-aaaa", "bounded search exhausted")
    return bank.all()


def test_merge_restamps_marker_over_finalizer_echo(tmp_path) -> None:
    entries = _marked_entries(tmp_path)
    result = {
        "verdict": "FAIL",
        "criteria": [{"criterion": "Ship the fix", "met": False, "evidence": "echo"}],
        "findings": [],
    }
    merged = cb.merge_finalizer_with_bank(
        result, ["Ship the fix"], entries, id_by_text={"Ship the fix": "c00-aaaa"}
    )
    (record,) = merged["criteria"]
    assert record["evidence_sufficient"] is False


def test_merge_strips_model_minted_marker_when_bank_is_bare(tmp_path) -> None:
    bank = _bank(tmp_path)
    bank.make_record_tool()("c00-aaaa", False, "refuted")
    result = {
        "verdict": "FAIL",
        "criteria": [
            {
                "criterion": "Ship the fix",
                "met": False,
                "evidence": "e",
                "evidence_sufficient": False,
            }
        ],
        "findings": [],
    }
    merged = cb.merge_finalizer_with_bank(
        result, ["Ship the fix"], bank.all(), id_by_text={"Ship the fix": "c00-aaaa"}
    )
    (record,) = merged["criteria"]
    assert "evidence_sufficient" not in record


def test_merge_strips_model_minted_marker_on_met_true(tmp_path) -> None:
    entries = _marked_entries(tmp_path)
    result = {
        "verdict": "PASS",
        "criteria": [
            {
                "criterion": "Ship the fix",
                "met": True,
                "evidence": "found it",
                "evidence_sufficient": False,
            }
        ],
        "findings": [],
    }
    merged = cb.merge_finalizer_with_bank(
        result, ["Ship the fix"], entries, id_by_text={"Ship the fix": "c00-aaaa"}
    )
    (record,) = merged["criteria"]
    assert record["met"] is True and "evidence_sufficient" not in record


def test_merge_bank_fallback_branch_carries_marker(tmp_path) -> None:
    """A criterion the finalizer OMITTED is backfilled from the bank WITH its marker."""
    entries = _marked_entries(tmp_path)
    result = {"verdict": "FAIL", "criteria": [], "findings": []}
    merged = cb.merge_finalizer_with_bank(
        result, ["Ship the fix"], entries, id_by_text={"Ship the fix": "c00-aaaa"}
    )
    (record,) = merged["criteria"]
    assert record["met"] is False and record["evidence_sufficient"] is False
    (finding,) = merged["findings"]
    assert "insufficient" in finding["detail"].casefold()


def test_merge_placeholder_branch_carries_marker() -> None:
    result = {"verdict": "FAIL", "criteria": [], "findings": []}
    merged = cb.merge_finalizer_with_bank(
        result, ["Ship the fix"], {}, id_by_text={"Ship the fix": "c00-aaaa"}
    )
    (record,) = merged["criteria"]
    assert record["evidence_sufficient"] is False and record["exhausted"] is True
    (finding,) = merged["findings"]
    assert "insufficient" in finding["detail"].casefold()


def test_merge_genuine_refutation_finding_stays_bare(tmp_path) -> None:
    bank = _bank(tmp_path)
    bank.make_record_tool()("c00-aaaa", False, "refuted")
    result = {"verdict": "FAIL", "criteria": [], "findings": []}
    merged = cb.merge_finalizer_with_bank(
        result, ["Ship the fix"], bank.all(), id_by_text={"Ship the fix": "c00-aaaa"}
    )
    (record,) = merged["criteria"]
    assert "evidence_sufficient" not in record
    (finding,) = merged["findings"]
    assert "insufficient" not in finding["detail"].casefold()


# ── recovery seams ───────────────────────────────────────────────────────────────────
def test_validate_coverage_passes_marker_records_rejects_untyped() -> None:
    ids = cb.criterion_id_map(["Ship the fix"])
    ok = {
        "verdict": "FAIL",
        "criteria": [
            {
                "criterion": "Ship the fix",
                "met": False,
                "evidence_sufficient": False,
            }
        ],
    }
    ccrit._validate_coverage(ok, ["Ship the fix"], ids)  # marker passes through
    from rebar.llm.errors import CompletionRecoveryError

    bad = {
        "verdict": "FAIL",
        "criteria": [{"criterion": "Ship the fix", "met": "false"}],
    }
    with pytest.raises(CompletionRecoveryError):
        ccrit._validate_coverage(bad, ["Ship the fix"], ids)


def test_finalizer_banked_evidence_payload_carries_marker(tmp_path) -> None:
    """The finalizer's banked_evidence input surfaces the marker for marked entries and
    omits the key entirely for bare ones."""
    bank = _bank(tmp_path)
    bank.record_insufficient("c00-aaaa", "bounded search exhausted")
    bank.make_record_tool()("c01-bbbb", False, "refuted")
    payload = cr._banked_evidence_payload(bank.all())
    by_id = {rec["criterion_id"]: rec for rec in payload}
    assert by_id["c00-aaaa"]["evidence_sufficient"] is False
    assert "evidence_sufficient" not in by_id["c01-bbbb"]


# ── reconcile_verdict: top-level derivation + marker-aware remediation ───────────────
def test_reconcile_derives_top_level_marker_and_remediation() -> None:
    from rebar.llm.completion import (
        COMPLETION_REMEDIATION_GUIDANCE,
        INSUFFICIENT_EVIDENCE_REMEDIATION,
        reconcile_verdict,
    )

    result = {
        "verdict": "FAIL",
        "findings": [{"criterion": "Ship the fix", "detail": "insufficient", "severity": "high"}],
        "criteria": [
            {"criterion": "Ship the fix", "met": False, "evidence_sufficient": False},
            {"criterion": "Docs updated", "met": True},
        ],
    }
    reconcile_verdict(result)
    assert result["verdict"] == "FAIL"
    assert result["evidence_sufficient"] is False
    assert result["remediation"] == INSUFFICIENT_EVIDENCE_REMEDIATION
    assert result["remediation"] != COMPLETION_REMEDIATION_GUIDANCE
    # Operator-directed steering (field incidents): exhaustion is not refutation; the fix is
    # an UNTAGGED comment citing exact tests/paths/SHAs + re-verify — never misclassifying
    # code-verifiable criteria as [non-codebase] to satisfy an exhausted search.
    guidance = result["remediation"].casefold()
    assert "not refutation" in guidance
    assert "untagged comment" in guidance
    assert "test function" in guidance and "file paths" in guidance and "sha" in guidance
    assert "re-verify" in guidance
    assert "`[non-codebase]` tag is reserved" in guidance
    assert "reserved for evidence that inherently lives outside" in guidance
    assert "so it is not non-codebase" in guidance


def test_reconcile_no_marker_when_any_criterion_genuinely_unmet() -> None:
    from rebar.llm.completion import COMPLETION_REMEDIATION_GUIDANCE, reconcile_verdict

    result = {
        "verdict": "FAIL",
        "findings": [{"criterion": "A", "detail": "d", "severity": "high"}],
        "criteria": [
            {"criterion": "A", "met": False},  # genuine refutation
            {"criterion": "B", "met": False, "evidence_sufficient": False},
        ],
    }
    reconcile_verdict(result)
    assert "evidence_sufficient" not in result
    assert result["remediation"] == COMPLETION_REMEDIATION_GUIDANCE


def test_reconcile_pass_clears_stale_top_level_marker() -> None:
    from rebar.llm.completion import reconcile_verdict

    result = {"verdict": "PASS", "findings": [], "evidence_sufficient": False}
    reconcile_verdict(result)
    assert result["verdict"] == "PASS"
    assert "evidence_sufficient" not in result
    assert "remediation" not in result


def test_reconcile_model_cannot_mint_top_level_marker() -> None:
    """A model-supplied top-level marker with NO marked criteria records is stripped —
    the derivation from criteria is authoritative."""
    from rebar.llm.completion import reconcile_verdict

    result = {
        "verdict": "FAIL",
        "findings": [{"criterion": "A", "detail": "d", "severity": "high"}],
        "criteria": [{"criterion": "A", "met": False}],
        "evidence_sufficient": False,
    }
    reconcile_verdict(result)
    assert "evidence_sufficient" not in result


def test_reconcile_verifier_fault_path_unchanged() -> None:
    """The 2a6f no-verdict fault path is untouched: no criteria markers, no top-level
    insufficiency marker, verdict_obtainable=False still set."""
    from rebar.llm.completion import reconcile_verdict

    result = {"verdict": "FAIL", "findings": [], "criteria": []}
    reconcile_verdict(result)
    assert result["verdict_obtainable"] is False
    assert "evidence_sufficient" not in result


# ── close gate refusal: fail-closed, honest wording ──────────────────────────────────
def test_close_refusal_message_reports_insufficient_evidence() -> None:
    from rebar._commands.close_precheck import _verification_fail_message

    result = {"evidence_sufficient": False, "remediation": "do the thing"}
    items = [{"criterion": "Ship the fix", "detail": "insufficient evidence"}]
    lines = ["  - Ship the fix: insufficient evidence"]
    message = _verification_fail_message("T-1", "task", result, items, lines)
    assert "insufficient evidence" in message.casefold()
    assert "unmet" not in message.split("\n")[0].casefold()
    assert "not closing" in message
    assert "do the thing" in message


def test_close_refusal_message_genuine_fail_unchanged() -> None:
    from rebar._commands.close_precheck import _verification_fail_message

    result = {"remediation": "guidance"}
    items = [{"criterion": "A", "detail": "d"}, {"criterion": "B", "detail": "e"}]
    lines = ["  - A: d", "  - B: e"]
    message = _verification_fail_message("T-1", "task", result, items, lines)
    assert "2 unmet criteria; not closing." in message
    assert "guidance" in message


# ── renderer wording ─────────────────────────────────────────────────────────────────
def test_render_verdict_text_insufficient_wording(capsys) -> None:
    from rebar._cli._llm_commands import _render_verdict_text

    base = {
        "target": {"kind": "ticket", "ticket_ids": ["T-1"]},
        "verdict": "FAIL",
        "findings": [{"criterion": "Ship the fix", "detail": "d", "severity": "high"}],
    }
    _render_verdict_text({**base, "evidence_sufficient": False})
    out = capsys.readouterr().out
    assert "evidence insufficient for 1 criterion" in out
    assert "unmet" not in out
    _render_verdict_text(base)
    out = capsys.readouterr().out
    assert "1 unmet criterion:" in out


# ── sidecar durability ───────────────────────────────────────────────────────────────
def test_sidecar_fail_payload_roundtrips_markers() -> None:
    from rebar.llm import completion_sidecar as cs

    verdict = {
        "verdict": "FAIL",
        "ticket_id": "T-1",
        "findings": [{"criterion": "Ship the fix", "detail": "d", "severity": "high"}],
        "criteria": [{"criterion": "Ship the fix", "met": False, "evidence_sufficient": False}],
        "evidence_sufficient": False,
        "remediation": "r",
    }
    payload = cs.build_payload(verdict)
    assert payload["schema"] == cs.SCHEMA
    assert payload["evidence_sufficient"] is False
    (record,) = payload["criteria"]
    assert record["evidence_sufficient"] is False
    # Absent marker → prior FAIL payload shape (key omitted, criteria still captured).
    bare = cs.build_payload(
        {
            "verdict": "FAIL",
            "ticket_id": "T-1",
            "findings": [{"criterion": "A", "detail": "d", "severity": "high"}],
            "criteria": [{"criterion": "A", "met": False}],
        }
    )
    assert "evidence_sufficient" not in bare


# ── schema + finalizer prompt alignment ──────────────────────────────────────────────
def _schema() -> dict:
    root = Path(__file__).resolve().parents[3]
    return json.loads(
        (root / "src/rebar/schemas/completion_verdict.schema.json").read_text(encoding="utf-8")
    )


def test_schema_declares_optional_markers() -> None:
    schema = _schema()
    top = schema["properties"]["evidence_sufficient"]
    assert top["const"] is False
    per = schema["properties"]["criteria"]["items"]["properties"]["evidence_sufficient"]
    assert per["const"] is False
    # OPTIONAL: neither level is required.
    assert "evidence_sufficient" not in schema.get("required", [])


def test_finalizer_prompt_states_sufficiency_rule() -> None:
    root = Path(__file__).resolve().parents[3]
    text = (root / "src/rebar/llm/reviewers/completion_verifier_finalizer.md").read_text(
        encoding="utf-8"
    )
    lowered = text.casefold()
    assert "evidence_sufficient" in lowered
    assert "insufficient" in lowered
