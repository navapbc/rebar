"""The normalized three-valued evidence contract (epic 8f6c / story 0b2b).

This is the SHARED data model every code-grounding backend (Engine A refutation,
T0 dependency existence, Engine B detectors) emits — authored once here so the
two consuming review efforts (`5fd2` DET floor, `9da1` reviewers) get one
implementation, not two.

Contract (locked):

* **Three-valued, confirm-only.** Every probe returns ONE evidence record whose
  ``outcome`` is ``refuted`` / ``match`` (resolved) or ``abstain`` (with a CLOSED
  structured ``reason``). On the resolution lane the resolved value is ``refuted``
  — we DISPROVE an asserted absence; we never assert an absence. So the oracle
  reduces false positives; it never manufactures confirmations.
* **Match and abstain share ONE shape.** A skipped backend uses the same record
  with ``outcome=abstain`` + ``coverage.status=skipped`` — the visible skip IS the
  coverage record (never a silent no-op).
* **CLOSED reason enum.** No open ``…``: ``version_skew`` and ``invalid_detector``
  are first-class; ``other`` is the explicit catch-all.

The JSON Schema in ``rebar/schemas/grounding.schema.json`` is the canonical source
of truth; this module builds + normalizes records to that shape. stdlib-only and
import-clean (a non-adopting client pays nothing).
"""

from __future__ import annotations

from typing import Any

# ── Closed vocabularies (mirror grounding.schema.json $defs) ─────────────────

#: CLOSED structured abstention reasons (SMT unknown-with-reason model).
ABSTAIN_REASONS: frozenset[str] = frozenset(
    {
        "unsupported_lang",
        "no_tool",
        "parse_error",
        "timeout",
        "ambiguous",
        "private_or_internal_suspected",
        "network_error",
        "rate_limited",
        "version_skew",
        "invalid_detector",
        "other",
    }
)

#: The three-valued outcome vocabulary.
OUTCOME_REFUTED = "refuted"
OUTCOME_MATCH = "match"
OUTCOME_ABSTAIN = "abstain"
OUTCOMES: frozenset[str] = frozenset({OUTCOME_REFUTED, OUTCOME_MATCH, OUTCOME_ABSTAIN})

#: The three oracle jobs.
JOB_REFUTE = "refute"
JOB_APPLIES = "applies"
JOB_SMELL = "smell"
JOBS: frozenset[str] = frozenset({JOB_REFUTE, JOB_APPLIES, JOB_SMELL})

#: Provenance tiers.
TIER_T0 = "T0"
TIER_T1 = "T1"
TIER_T2 = "T2"
TIERS: frozenset[str] = frozenset({TIER_T0, TIER_T1, TIER_T2})

#: Coverage statuses.
STATUS_RAN = "ran"
STATUS_SKIPPED = "skipped"
STATUSES: frozenset[str] = frozenset({STATUS_RAN, STATUS_SKIPPED})

#: The fallback reason when an unknown/missing reason is normalized.
DEFAULT_REASON = "other"


class GroundingContractError(ValueError):
    """A required evidence field is missing or outside its closed vocabulary.

    Raised by the strict constructors below (NOT by the lenient
    :func:`normalize_evidence`, which clamps rather than raises) so a backend that
    hand-builds a malformed record fails loudly at the boundary.
    """


def _drop_nulls(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# ── Builders ─────────────────────────────────────────────────────────────────


def coverage(
    *,
    backend: str,
    status: str,
    version: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build a single-backend coverage record.

    ``status=skipped`` MUST carry a ``reason`` (the skip's coverage record); a
    ``ran`` record MUST NOT. A reason outside :data:`ABSTAIN_REASONS` is rejected.
    """
    if status not in STATUSES:
        raise GroundingContractError(f"coverage status {status!r} not in {sorted(STATUSES)}")
    if status == STATUS_SKIPPED and reason is None:
        raise GroundingContractError("a skipped coverage record requires a reason")
    if status == STATUS_RAN and reason is not None:
        raise GroundingContractError("a ran coverage record must not carry a skip reason")
    if reason is not None and reason not in ABSTAIN_REASONS:
        raise GroundingContractError(f"reason {reason!r} not in the closed set {sorted(ABSTAIN_REASONS)}")
    return _drop_nulls({"backend": backend, "status": status, "version": version, "reason": reason})


def _resolved(
    outcome: str,
    *,
    job: str,
    provenance_tier: str,
    coverage: dict[str, Any],
    detector_id: str | None = None,
    reference: dict[str, Any] | None = None,
    location: dict[str, Any] | None = None,
    attention_only: bool = False,
    detail: str | None = None,
) -> dict[str, Any]:
    if job not in JOBS:
        raise GroundingContractError(f"job {job!r} not in {sorted(JOBS)}")
    if provenance_tier not in TIERS:
        raise GroundingContractError(f"provenance_tier {provenance_tier!r} not in {sorted(TIERS)}")
    if not isinstance(coverage, dict) or coverage.get("backend") is None:
        raise GroundingContractError("a resolved record requires a coverage dict with a backend")
    rec: dict[str, Any] = {
        "outcome": outcome,
        "job": job,
        "provenance_tier": provenance_tier,
        "reason": None,
        "detector_id": detector_id,
        "reference": reference,
        "location": location,
        "coverage": coverage,
        "detail": detail,
    }
    if attention_only:
        rec["attention_only"] = True
    return _drop_nulls(rec)


def refuted(
    *,
    job: str = JOB_REFUTE,
    provenance_tier: str,
    coverage: dict[str, Any],
    reference: dict[str, Any] | None = None,
    location: dict[str, Any] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Build a ``refuted`` record — an asserted absence was DISPROVED.

    This is the only resolved value the resolution lane ever emits (never an
    asserted absence). ``location`` is the definition site that disproves the
    claim, when there is one.
    """
    return _resolved(
        OUTCOME_REFUTED,
        job=job,
        provenance_tier=provenance_tier,
        coverage=coverage,
        reference=reference,
        location=location,
        detail=detail,
    )


def match(
    *,
    job: str,
    provenance_tier: str,
    coverage: dict[str, Any],
    detector_id: str | None = None,
    location: dict[str, Any] | None = None,
    attention_only: bool = False,
    detail: str | None = None,
) -> dict[str, Any]:
    """Build a ``match`` record — a detector (Engine B) matched the repo."""
    return _resolved(
        OUTCOME_MATCH,
        job=job,
        provenance_tier=provenance_tier,
        coverage=coverage,
        detector_id=detector_id,
        location=location,
        attention_only=attention_only,
        detail=detail,
    )


def abstain(
    reason: str,
    *,
    job: str,
    provenance_tier: str,
    coverage: dict[str, Any] | None = None,
    backend: str | None = None,
    version: str | None = None,
    detector_id: str | None = None,
    reference: dict[str, Any] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Build an ``abstain`` record — a fail-open skip with a recorded reason.

    The IRONCLAD invariant: unsupported lang / missing tool / crash / timeout /
    version-skew / invalid-detector becomes one of these, never a raise and never
    an asserted absence. Pass either a ready-made ``coverage`` dict or a
    ``backend`` (+ optional ``version``) and the matching ``skipped`` coverage
    record is synthesized for you.
    """
    if reason not in ABSTAIN_REASONS:
        raise GroundingContractError(f"reason {reason!r} not in the closed set {sorted(ABSTAIN_REASONS)}")
    if job not in JOBS:
        raise GroundingContractError(f"job {job!r} not in {sorted(JOBS)}")
    if provenance_tier not in TIERS:
        raise GroundingContractError(f"provenance_tier {provenance_tier!r} not in {sorted(TIERS)}")
    if coverage is None:
        if backend is None:
            raise GroundingContractError("abstain() needs a coverage dict or a backend to synthesize one")
        coverage = _skipped_coverage(backend, version, reason)
    return _drop_nulls(
        {
            "outcome": OUTCOME_ABSTAIN,
            "job": job,
            "provenance_tier": provenance_tier,
            "reason": reason,
            "detector_id": detector_id,
            "reference": reference,
            "location": None,
            "coverage": coverage,
            "detail": detail,
        }
    )


def _skipped_coverage(backend: str, version: str | None, reason: str) -> dict[str, Any]:
    return _drop_nulls({"backend": backend, "status": STATUS_SKIPPED, "version": version, "reason": reason})


# ── Normalization (lenient, never raises) ────────────────────────────────────


def normalize_evidence(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a raw dict into the canonical evidence shape — best-effort, clamping.

    Unlike the strict builders this NEVER raises (fail-open all the way down): an
    unknown ``outcome`` clamps to ``abstain``; an abstain with an unknown/missing
    ``reason`` clamps to ``other``; unknown ``job``/``provenance_tier`` clamp to
    ``refute``/``T1``; a missing coverage gets a minimal skipped record. Use this
    when ingesting evidence from an untrusted boundary (e.g. parsed SARIF).
    """
    e = dict(raw)
    outcome = str(e.get("outcome", "")).strip()
    if outcome not in OUTCOMES:
        outcome = OUTCOME_ABSTAIN
    e["outcome"] = outcome

    job = str(e.get("job", "")).strip()
    e["job"] = job if job in JOBS else JOB_REFUTE

    tier = str(e.get("provenance_tier", "")).strip()
    e["provenance_tier"] = tier if tier in TIERS else TIER_T1

    if outcome == OUTCOME_ABSTAIN:
        reason = e.get("reason")
        e["reason"] = reason if reason in ABSTAIN_REASONS else DEFAULT_REASON
    else:
        e["reason"] = None

    raw_cov = e.get("coverage")
    cov: dict[str, Any] = raw_cov if isinstance(raw_cov, dict) else {}
    backend = cov.get("backend") or "unknown"
    status = cov.get("status")
    if status not in STATUSES:
        status = STATUS_SKIPPED if outcome == OUTCOME_ABSTAIN else STATUS_RAN
    repaired: dict[str, Any] = {"backend": backend, "status": status}
    if cov.get("version") is not None:
        repaired["version"] = cov["version"]
    if status == STATUS_SKIPPED:
        cov_reason = cov.get("reason")
        repaired["reason"] = cov_reason if cov_reason in ABSTAIN_REASONS else (e["reason"] or DEFAULT_REASON)
    e["coverage"] = repaired

    return _drop_nulls(e)


def is_resolved(evidence: dict[str, Any]) -> bool:
    """True iff the record carries a resolution (``refuted``/``match``), not an abstain."""
    return evidence.get("outcome") in (OUTCOME_REFUTED, OUTCOME_MATCH)


def validate(evidence: dict[str, Any]) -> None:
    """Validate a record against the canonical JSON Schema (raises on mismatch).

    Imports :mod:`rebar.schemas` lazily so this module stays import-clean for
    non-validating callers; ``jsonschema`` is only needed to validate (the ``dev``
    extra), not to build evidence.
    """
    from rebar import schemas

    schemas.validator(schemas.GROUNDING).validate(evidence)
