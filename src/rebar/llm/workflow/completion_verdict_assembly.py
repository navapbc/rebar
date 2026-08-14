"""Full-coverage completion-verdict assembly from the criterion bank.

Extracted from ``completion_banking`` along its existing call-graph seam (the two rendering
seams plus their shared helpers); ``completion_banking`` re-exports the public names so
callers are unchanged. Both seams understand the framework-set ``evidence_sufficient: false``
sibling marker (ticket 1d71): a banked entry carrying it means the bounded evidence search
was EXHAUSTED without finding evidence — insufficiency, not a refutation — so the assembled
criteria records carry the marker and the findings say so, while the verdict vocabulary
stays {PASS, FAIL} and ``met`` stays bool (every fail-closed consumer is untouched).
"""

from __future__ import annotations

from typing import Any

_FALLBACK_FINALIZER = "deterministic_fallback"

# Finding text for an insufficiency (marker-carrying) record vs a genuine banked refutation.
INSUFFICIENT_BANKED_DETAIL = (
    "insufficient evidence: the bounded evidence search was exhausted without demonstrating "
    "this criterion — an evidence gap, not a refutation."
)
INSUFFICIENT_UNVERIFIED_DETAIL = (
    "criterion was never verified (recovery budget exhausted); insufficient evidence, not a "
    "refutation — recorded as an unverified placeholder."
)


def _ids_for(criteria: list[str], id_by_text: dict[str, str] | None) -> dict[str, str]:
    if id_by_text is not None:
        return id_by_text
    # Lazy import: completion_banking imports this module for re-export, so a top-level
    # import back into it would be circular.
    from rebar.llm.workflow.completion_banking import criterion_id_map

    return criterion_id_map(criteria)


def _entry_insufficient(entry: dict[str, Any] | None) -> bool:
    """True when a bank entry is the bounded fallback's insufficiency record."""
    return entry is not None and entry.get("evidence_sufficient") is False


def assemble_deterministic_verdict(
    ticket_id: str,
    criteria: list[str],
    bank_entries: dict[str, dict[str, Any]],
    *,
    id_by_text: dict[str, str] | None = None,
    runner: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Assemble a FULL-COVERAGE completion verdict DIRECTLY from the bank — no model call.

    Used when the LLM finalizer fails twice. Banked ``met`` flags are taken as-is; unbanked
    criteria become ``met=false`` unverified/exhausted placeholders. Cross-criterion downgrade
    authority never ran, so it is recorded as SKIPPED and the verdict is stamped
    ``finalizer="deterministic_fallback"`` with ``certifiable=False`` — the completion sidecar
    reads that field so the signing path WITHHOLDS a certified signature. This is the ticket's
    OWN self-verdict provenance (distinct from ``child_closure_findings``). A run with any
    banked progress can never die verdict-less. Insufficiency (a marker-carrying bank entry,
    or an unbanked placeholder — never verified at all) is rendered AS insufficiency.
    """
    ids = _ids_for(criteria, id_by_text)
    criteria_records: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    any_unmet = False
    for text in criteria:
        cid = ids[text]
        entry = bank_entries.get(cid)
        if entry is not None:
            met = bool(entry.get("met"))
            record = {
                "criterion": text,
                "met": met,
                "criterion_id": cid,
                "evidence": entry.get("evidence") or "",
                "truncated": bool(entry.get("truncated")),
            }
            insufficient = _entry_insufficient(entry)
            if insufficient:
                record["evidence_sufficient"] = False
            criteria_records.append(record)
            if not met:
                any_unmet = True
                findings.append(
                    {
                        "criterion": text,
                        "detail": INSUFFICIENT_BANKED_DETAIL
                        if insufficient
                        else "banked verdict: criterion not met.",
                        "severity": "high",
                        "citations": [],
                    }
                )
        else:
            any_unmet = True
            criteria_records.append(
                {
                    "criterion": text,
                    "met": False,
                    "criterion_id": cid,
                    "evidence": "",
                    "unverified": True,
                    "exhausted": True,
                    "evidence_sufficient": False,
                }
            )
            findings.append(
                {
                    "criterion": text,
                    "detail": INSUFFICIENT_UNVERIFIED_DETAIL,
                    "severity": "high",
                    "citations": [],
                }
            )
    return {
        "verdict": "FAIL" if any_unmet else "PASS",
        "findings": findings,
        "criteria": criteria_records,
        "summary": "Assembled deterministically from banked verdicts after the finalizer "
        "failed; unverified criteria are met=false placeholders.",
        "target": {"kind": "ticket", "ticket_ids": [ticket_id]},
        "reviewers": ["completion-verifier"],
        "finalizer": _FALLBACK_FINALIZER,
        "downgrade_authority": "skipped",
        "certifiable": False,
        "runner": runner or "deterministic_fallback",
        "model": model or "deterministic_fallback",
        "trace_id": None,
        "provider_provenance": None,
    }


def _restamp_marker(record: dict[str, Any], entry: dict[str, Any] | None) -> dict[str, Any]:
    """Authoritatively re-stamp the insufficiency marker from the BANK onto a finalizer echo.

    Model output never owns the marker: a bank entry carrying it stamps a still-unmet echo;
    any model-minted marker over a bare bank entry (or a met=true record) is stripped."""
    if record.get("met") is False and _entry_insufficient(entry):
        record["evidence_sufficient"] = False
    else:
        record.pop("evidence_sufficient", None)
    return record


def merge_finalizer_with_bank(
    result: dict[str, Any],
    criteria: list[str],
    bank_entries: dict[str, dict[str, Any]],
    *,
    id_by_text: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Backfill a full-coverage verdict from an LLM finalizer ``result``.

    The LLM finalizer keys by verbatim criterion; any criterion it omitted is backfilled from
    the bank (or as a met=false unverified placeholder), guaranteeing full coverage while
    preserving the finalizer's own judgments (including a cross-criterion downgrade of a
    banked met=true). The bank is the source of truth for the ``evidence_sufficient`` marker:
    every record — echoed or backfilled — is re-stamped from its bank entry.
    """
    ids = _ids_for(criteria, id_by_text)
    by_text: dict[str, dict[str, Any]] = {}
    for record in result.get("criteria") or []:
        if isinstance(record, dict) and record.get("criterion"):
            by_text[str(record["criterion"]).strip()] = record
    records: list[dict[str, Any]] = []
    for text in criteria:
        existing = by_text.get(text.strip())
        entry = bank_entries.get(ids[text])
        if isinstance(existing, dict) and isinstance(existing.get("met"), bool):
            records.append(
                _restamp_marker({**existing, "criterion": text, "criterion_id": ids[text]}, entry)
            )
            continue
        if entry is not None:
            record = {
                "criterion": text,
                "met": bool(entry.get("met")),
                "criterion_id": ids[text],
                "evidence": entry.get("evidence") or "",
            }
            if _entry_insufficient(entry):
                record["evidence_sufficient"] = False
            records.append(record)
        else:
            records.append(
                {
                    "criterion": text,
                    "met": False,
                    "criterion_id": ids[text],
                    "unverified": True,
                    "exhausted": True,
                    "evidence_sufficient": False,
                }
            )
    any_unmet = any(not r["met"] for r in records)
    merged = dict(result)
    merged["criteria"] = records
    merged["verdict"] = "FAIL" if any_unmet else str(result.get("verdict") or "PASS").upper()
    merged.setdefault("target", {"kind": "ticket", "ticket_ids": [ticket_id_of(result)]})
    merged.setdefault("reviewers", ["completion-verifier"])
    # A real LLM finalizer ran and reconciled the full-coverage verdict — it is certifiable.
    merged.setdefault("finalizer", "llm_finalizer")
    merged.setdefault("certifiable", True)
    if any_unmet:
        merged["findings"] = _findings_for_unmet(records, result.get("findings"))
    else:
        merged["findings"] = []
    return merged


def ticket_id_of(result: dict[str, Any]) -> str:
    target = result.get("target")
    if isinstance(target, dict):
        ids = target.get("ticket_ids")
        if isinstance(ids, list) and ids:
            return str(ids[0])
    return ""


def _findings_for_unmet(records: list[dict[str, Any]], existing: Any) -> list[dict[str, Any]]:
    """Preserve the finalizer's own findings, adding a placeholder finding for any unmet
    criterion it did not itself report (so a FAIL always names every unmet criterion). A
    marker-carrying record's placeholder says insufficiency, not refutation."""
    by_criterion: dict[str, dict[str, Any]] = {}
    if isinstance(existing, list):
        for f in existing:
            if isinstance(f, dict) and f.get("criterion"):
                by_criterion[str(f["criterion"]).strip()] = f
    out: list[dict[str, Any]] = []
    for record in records:
        if record["met"]:
            continue
        text = str(record["criterion"]).strip()
        if text in by_criterion:
            out.append(by_criterion[text])
        else:
            out.append(
                {
                    "criterion": record["criterion"],
                    "detail": INSUFFICIENT_UNVERIFIED_DETAIL
                    if record.get("evidence_sufficient") is False
                    else "criterion not met (banked/unverified).",
                    "severity": "high",
                    "citations": [],
                }
            )
    return out


__all__ = [
    "INSUFFICIENT_BANKED_DETAIL",
    "INSUFFICIENT_UNVERIFIED_DETAIL",
    "assemble_deterministic_verdict",
    "merge_finalizer_with_bank",
    "ticket_id_of",
]
