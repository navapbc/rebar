"""SARIF 2.1.0 interchange at the edges (epic 8f6c / story 0b2b).

One normalized evidence model lives internally (:mod:`rebar.grounding.evidence`);
SARIF 2.1.0 is used for interchange/ingest — OpenGrep/semgrep emit it — but our
three-valued model is a SUPERSET (SARIF has no native ``refuted`` / ``abstain`` /
coverage), so we map to/from SARIF only at the boundary.

The mapping (de-risked in the spike, E5: SARIF result + ``rule.properties``):

* **from_sarif** — a SARIF result + its rule's ``properties`` (carrying a
  ``rebar_envelope``) → one normalized ``match`` record. A ``rebar`` property bag
  written by :func:`to_sarif` is preferred when present (round-trip fidelity),
  otherwise the envelope/kind are read.
* **to_sarif** — evidence records → a minimal SARIF log. ``match`` becomes a
  SARIF ``result`` (``kind`` = informational/review); the rebar-only fields
  (outcome, reason, coverage, tier, job) ride in ``result.properties.rebar`` so a
  round-trip is lossless. ``refuted``/``abstain`` have no faithful SARIF result
  shape, so they are carried in ``properties.rebar`` too (``kind=notApplicable``
  for abstain) rather than silently dropped.

stdlib-only (json); import-clean.
"""

from __future__ import annotations

from typing import Any

from . import evidence as ev

_TOOL_NAME = "rebar-grounding"
_SARIF_VERSION = "2.1.0"
_SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

# SARIF result.kind vocabulary we use. abstain has no faithful native kind, so it
# maps to notApplicable and the real outcome rides in properties.rebar.
_KIND_FOR_OUTCOME = {
    ev.OUTCOME_MATCH: "review",
    ev.OUTCOME_REFUTED: "informational",
    ev.OUTCOME_ABSTAIN: "notApplicable",
}


def to_sarif(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Serialize evidence records to a minimal SARIF 2.1.0 log (lossless round-trip).

    Every record's full normalized shape rides in ``result.properties.rebar`` so
    :func:`from_sarif` reconstructs it exactly; the standard SARIF fields
    (``ruleId``, ``kind``, ``message``, ``locations``) are populated so external
    SARIF consumers still see something sensible.
    """
    results: list[dict[str, Any]] = []
    rule_ids: dict[str, dict[str, Any]] = {}
    for rec in records:
        rec = ev.normalize_evidence(rec)
        rid = rec.get("detector_id") or f"rebar.{rec['job']}.{rec['outcome']}"
        rule_ids.setdefault(rid, {"id": rid, "properties": {"rebar_envelope": {"tier": rec["provenance_tier"], "job": rec["job"]}}})
        result: dict[str, Any] = {
            "ruleId": rid,
            "kind": _KIND_FOR_OUTCOME.get(rec["outcome"], "review"),
            "level": "none",
            "message": {"text": rec.get("detail") or rec["outcome"]},
            "properties": {"rebar": rec},
        }
        loc = rec.get("location")
        if loc and loc.get("file"):
            region = {}
            if loc.get("line_start"):
                region["startLine"] = loc["line_start"]
            if loc.get("line_end"):
                region["endLine"] = loc["line_end"]
            phys: dict[str, Any] = {"artifactLocation": {"uri": loc["file"]}}
            if region:
                phys["region"] = region
            result["locations"] = [{"physicalLocation": phys}]
        results.append(result)
    return {
        "$schema": _SARIF_SCHEMA,
        "version": _SARIF_VERSION,
        "runs": [
            {
                "tool": {"driver": {"name": _TOOL_NAME, "rules": list(rule_ids.values())}},
                "results": results,
            }
        ],
    }


def from_sarif(
    sarif: dict[str, Any],
    *,
    backend: str = "opengrep",
    job: str = ev.JOB_SMELL,
    provenance_tier: str = ev.TIER_T1,
    version: str | None = None,
    trust_rebar_bag: bool = True,
) -> list[dict[str, Any]]:
    """Ingest a SARIF log into normalized evidence records.

    A ``properties.rebar`` bag written by :func:`to_sarif` is honored verbatim
    (lossless round-trip) — but ONLY when ``trust_rebar_bag`` is true. That bag is
    trustworthy only for SARIF WE produced; a foreign/untrusted SARIF producer could
    set ``properties.rebar`` to inject an arbitrary ``match``/``refuted`` with an
    attacker-chosen ``detector_id``/``location``, so pass ``trust_rebar_bag=False``
    when ingesting third-party SARIF — every result is then re-derived as a ``match``
    from the rule's ``properties.rebar_envelope`` (tier/job/attention_only) per the
    spike's E5 mapping, falling back to the passed defaults.
    """
    out: list[dict[str, Any]] = []
    for run in sarif.get("runs", []) or []:
        driver = (run.get("tool") or {}).get("driver") or {}
        rules = {r.get("id"): r for r in driver.get("rules", []) or []}
        for res in run.get("results", []) or []:
            rebar_bag = (res.get("properties") or {}).get("rebar")
            if trust_rebar_bag and isinstance(rebar_bag, dict):
                out.append(ev.normalize_evidence(rebar_bag))
                continue
            rid = res.get("ruleId") or res.get("check_id")
            envelope = ((rules.get(rid) or {}).get("properties") or {}).get("rebar_envelope") or {}
            loc = _first_location(res)
            out.append(
                ev.match(
                    job=str(envelope.get("job") or job),
                    provenance_tier=str(envelope.get("tier") or provenance_tier),
                    coverage=ev.coverage(backend=backend, status=ev.STATUS_RAN, version=version),
                    detector_id=rid,
                    location=loc,
                    attention_only=bool(envelope.get("attention_only", False)),
                    detail=_message_text(res),
                )
            )
    return out


def _first_location(res: dict[str, Any]) -> dict[str, Any] | None:
    locs = res.get("locations") or []
    if not locs:
        return None
    phys = (locs[0] or {}).get("physicalLocation") or {}
    uri = (phys.get("artifactLocation") or {}).get("uri")
    if not uri:
        return None
    out: dict[str, Any] = {"file": uri}
    region = phys.get("region") or {}
    if region.get("startLine"):
        out["line_start"] = region["startLine"]
    if region.get("endLine"):
        out["line_end"] = region["endLine"]
    return out


def _message_text(res: dict[str, Any]) -> str | None:
    msg = res.get("message")
    if isinstance(msg, dict):
        return msg.get("text")
    return msg if isinstance(msg, str) else None
