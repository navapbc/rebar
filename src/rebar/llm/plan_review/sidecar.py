"""The ``REVIEW_RESULT`` observability sidecar (child db7b).

Every plan review emits a ``REVIEW_RESULT`` event capturing the per-criterion
verdicts + finding fingerprints + metadata, so per-criterion FP / remediation
analysis can be reconstructed OFFLINE without taxing rebar's hot paths.

It is a **reducer-ignored** sidecar: ``REVIEW_RESULT`` is NOT in
``KNOWN_EVENT_TYPES``, so the reducer skips it (it never enters compiled state,
deps, validate, or the close/claim hot paths) and compaction PRESERVES it
(forward-compat payload, never absorbed into a SNAPSHOT). It IS in the write-path
allow-list (so it can be emitted) and in ``_NON_REPLAY_KNOWN_TYPES`` (so ``fsck``
recognises it and does not warn "newer than me"). This mirrors the SYNC /
PRECONDITIONS precedent. Like every event it follows the
preserved-and-ignored-by-older-clones rollout (upgrade reconcile hosts first).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

EVENT_TYPE = "REVIEW_RESULT"

# The impact-model formula version that produced this sidecar's scores (story
# raptorial-galloping-dragon). Stamped top-level so the calibration replay can SEGMENT
# old-formula vs new-formula findings and never pool across versions (the same cohort-tagging
# discipline as the per-finding `cohort` carrier; a MISSING tag reads as "unknown/skip" offline).
# Bump this whenever `decide.impact_plan` changes shape → a fresh calibration cohort.
IMPACT_MODEL_VERSION = "plan-v2"

# Retention bound (child db7b AC4). REVIEW_RESULT is reducer-IGNORED, so rebar's event
# COMPACTION intentionally PRESERVES it (never snapshots/absorbs a non-KNOWN type) —
# compaction therefore cannot bound its growth. A dedicated prune keeps the most-recent
# RETAIN sidecars per ticket (recent history for offline analysis; each review
# supersedes the prior, and prior runs were already captured at emit time) and removes
# older ones, bounding growth without touching the reducer/compaction hot paths.
#
# Bound raised 10 -> 50 (story fde0): drift-refresh re-reviews on one ticket can exceed 10
# rounds, so 10 dropped still-relevant recent history; 50 covers observed single-digit
# remediation-loop depth with headroom while still capping unbounded growth. This is the
# SINGLE definition of the cap — code_review.sidecar imports it (no second literal).
RETAIN_PER_TICKET = 50


def emit(verdict: dict[str, Any], *, material: str | None = None, repo_root=None) -> bool:
    """Append a ``REVIEW_RESULT`` sidecar event from a plan-review verdict, then prune
    to the retention bound. Returns True on success, False on any failure (the sidecar
    is observability — a failed emit must NEVER fail the review itself). Best-effort."""
    from rebar import config as _config
    from rebar._commands._seam import append_event

    try:
        tracker = _config.tracker_dir(repo_root)
        payload = build_payload(verdict, material=material, repo_root=repo_root)
        append_event(verdict["ticket_id"], EVENT_TYPE, payload, tracker, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — best-effort observability sidecar; broad-but-logged below, never fails the review
        # Observability floor: the sidecar is best-effort observability — a failed emit
        # must never fail the review, but the failure itself is a real signal worth a
        # stderr diagnostic (broad-but-logged; see rebar._logging).
        logger.warning("REVIEW_RESULT sidecar emit failed; continuing", exc_info=True)
        return False
    prune(verdict.get("ticket_id", ""), repo_root=repo_root)  # best-effort retention
    return True


def prune(ticket_id: str, *, keep: int = RETAIN_PER_TICKET, repo_root=None) -> int:
    """Bound REVIEW_RESULT growth: keep the most-recent ``keep`` sidecar events for a
    ticket (filename timestamp order) and remove older ones. Returns the count removed.
    Best-effort and exception-swallowing — a failed prune never fails the review; the
    sidecars are reducer-ignored, so removing old ones is safe (not state-bearing)."""
    try:
        import subprocess

        from rebar import config as _config
        from rebar._engine_support.resolver import resolve_ticket_id

        tracker = str(_config.tracker_dir(repo_root))
        rid = resolve_ticket_id(ticket_id, tracker) or ticket_id
        ticket_dir = os.path.join(tracker, rid)
        files = sorted(
            f
            for f in os.listdir(ticket_dir)
            if f.endswith(f"-{EVENT_TYPE}.json") and not f.startswith(".")
        )
        old = files[: max(0, len(files) - keep)]
        if not old:
            return 0
        rels = [f"{rid}/{f}" for f in old]
        subprocess.run(["git", "-C", tracker, "rm", "-q", *rels], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                tracker,
                "commit",
                "-q",
                "--no-verify",
                "-m",
                f"prune: REVIEW_RESULT sidecar for {rid} (retain {keep})",
            ],
            check=True,
            capture_output=True,
        )
        return len(old)
    except Exception:  # noqa: BLE001 — best-effort retention prune; broad-but-logged below, never fails the review
        # Best-effort retention; a failed prune never fails the review (sidecars are
        # reducer-ignored, so removing old ones is safe). Log the failure (floor).
        logger.warning("REVIEW_RESULT sidecar prune failed; continuing", exc_info=True)
        return 0


def latest_review_result(ticket_id: str, *, repo_root=None) -> dict[str, Any] | None:
    """Return the **most-recent** ``REVIEW_RESULT`` sidecar payload for ``ticket_id``,
    or ``None`` when none is usable.

    Contract (child e344) — this is the reader a remediation re-review uses to hand the
    Pass-2 novelty sub-call its own prior findings. It mirrors the sidecar's
    observability-only, best-effort posture and **never raises**, so a missing/garbled
    prior review degrades gracefully to "no prior findings":

    - Return value: the deserialized ``data`` payload of the latest sidecar event (the
      ``build_payload`` dict — ``schema``, ``findings``, ``coverage``, …), NOT the event
      envelope. Callers read ``result["findings"]`` directly.
    - No sidecar yet / ticket dir absent / empty dir → ``None`` (the common first-review
      case; the caller proceeds with no prior findings).
    - **Walk-back over unusable files:** the newest sidecar is preferred, but a single
      malformed (mid-emit crash) or foreign-schema newest file does NOT blind the caller
      to older valid reviews — the reader walks from newest to oldest and returns the
      first usable ``plan_review_result_v1``/``_v2`` payload (a malformed file is logged, once).
    - **Schema guard:** a payload whose ``schema`` is neither ``"plan_review_result_v1"``
      nor ``"plan_review_result_v2"`` is skipped, so a future schema bump can never feed a
      stale shape to the novelty sub-call. All files unusable → ``None``.
    """
    try:
        from rebar import config as _config
        from rebar._engine_support.resolver import resolve_ticket_id

        tracker = str(_config.tracker_dir(repo_root))
        rid = resolve_ticket_id(ticket_id, tracker) or ticket_id
        ticket_dir = os.path.join(tracker, rid)
        files = sorted(
            f
            for f in os.listdir(ticket_dir)
            if f.endswith(f"-{EVENT_TYPE}.json") and not f.startswith(".")
        )
        # Filenames are timestamp-prefixed (fixed-width ns epoch), so reverse order is
        # newest-first. Return the first USABLE v1 payload, tolerating a corrupt newest.
        for fname in reversed(files):
            try:
                with open(os.path.join(ticket_dir, fname), encoding="utf-8") as fh:
                    event = json.load(fh)
            except (OSError, ValueError):
                logger.warning("REVIEW_RESULT sidecar %s unreadable; trying older", fname)
                continue
            payload = event.get("data") if isinstance(event, dict) else None
            if isinstance(payload, dict) and payload.get("schema") in (
                "plan_review_result_v1",
                "plan_review_result_v2",
            ):
                return payload
        return None
    except FileNotFoundError:
        return None  # ticket dir absent → no prior review (common first-review case)
    except Exception:  # noqa: BLE001 — best-effort observability reader; broad-but-logged, never raises
        logger.warning(
            "REVIEW_RESULT sidecar read failed; treating as no prior review", exc_info=True
        )
        return None


def all_review_results(ticket_id: str, *, repo_root=None) -> list[dict[str, Any]]:
    """Return **all** retained ``REVIEW_RESULT`` sidecar payloads for ``ticket_id``,
    newest→oldest, as a list of usable ``build_payload`` dicts (``[]`` when none).

    The full-history analogue of :func:`latest_review_result` (story 46f0's audit read
    layer): same ticket-dir resolution, same schema guard (accepts both
    ``plan_review_result_v1`` and ``_v2``), same observability-only, best-effort posture —
    it **never raises**. Where ``latest_review_result`` returns the first usable payload,
    this returns every usable one, so an offline consumer can walk the retained
    plan-review history. Unreadable/foreign-schema files are skipped (logged once);
    a missing ticket dir or any error degrades to ``[]``."""
    try:
        from rebar import config as _config
        from rebar._engine_support.resolver import resolve_ticket_id

        tracker = str(_config.tracker_dir(repo_root))
        rid = resolve_ticket_id(ticket_id, tracker) or ticket_id
        ticket_dir = os.path.join(tracker, rid)
        files = sorted(
            f
            for f in os.listdir(ticket_dir)
            if f.endswith(f"-{EVENT_TYPE}.json") and not f.startswith(".")
        )
        # Filenames are timestamp-prefixed (fixed-width ns epoch), so reverse order is
        # newest-first. Collect every USABLE v1/v2 payload, tolerating corrupt files.
        out: list[dict[str, Any]] = []
        for fname in reversed(files):
            try:
                with open(os.path.join(ticket_dir, fname), encoding="utf-8") as fh:
                    event = json.load(fh)
            except (OSError, ValueError):
                logger.warning("REVIEW_RESULT sidecar %s unreadable; skipping", fname)
                continue
            payload = event.get("data") if isinstance(event, dict) else None
            if isinstance(payload, dict) and payload.get("schema") in (
                "plan_review_result_v1",
                "plan_review_result_v2",
            ):
                out.append(payload)
        return out
    except FileNotFoundError:
        return []  # ticket dir absent → no retained history
    except Exception:  # noqa: BLE001 — best-effort observability reader; broad-but-logged, never raises
        logger.warning(
            "REVIEW_RESULT sidecar history read failed; treating as no history", exc_info=True
        )
        return []


def latest_review_timestamp(ticket_id: str, *, repo_root=None) -> int | None:
    """Return the nanosecond timestamp of the **most-recent** ``REVIEW_RESULT`` sidecar for
    ``ticket_id`` (the "last review of ANY kind" marker — every review, PASS or BLOCK, emits a
    sidecar), or ``None`` when none exists / on any error.

    The remediation-mode freshness window (child ec89) measures from this. The timestamp is the
    filename's ns prefix (``<ts_ns>-<uuid>-REVIEW_RESULT.json`` — see ``event_append``), so this
    needs no JSON parse. Best-effort and never raises, matching the sidecar's observability
    posture."""
    try:
        from rebar import config as _config
        from rebar._engine_support.resolver import resolve_ticket_id

        tracker = str(_config.tracker_dir(repo_root))
        rid = resolve_ticket_id(ticket_id, tracker) or ticket_id
        ticket_dir = os.path.join(tracker, rid)
        files = sorted(
            f
            for f in os.listdir(ticket_dir)
            if f.endswith(f"-{EVENT_TYPE}.json") and not f.startswith(".")
        )
        if not files:
            return None
        prefix = files[-1].split("-", 1)[0]  # the ns-epoch timestamp prefix of the newest file
        return int(prefix)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — best-effort observability reader; broad-but-logged, never raises
        logger.warning(
            "REVIEW_RESULT sidecar timestamp read failed; treating as none", exc_info=True
        )
        return None


# ── normalized finding fingerprint (OBSERVABILITY-ONLY — sidecar payload, never the
#    surfaced verdict) ──────────────────────────────────────────────────────────────
# The caller-visible finding ``id`` (orchestrator.mint_finding_id) hashes the EXACT
# finding text, so the Pass-1 finder re-wording the same defect on a re-review mints a
# DIFFERENT id — which makes "did this finding survive a revision?" unmeasurable from the
# exact id alone (it reads as ~100% resolved for every LLM criterion, vs the deterministic
# floor where the text is stable). ``norm_id`` is a coarser, reword-tolerant fingerprint
# (significant-token set + criteria, order-insensitive) so offline calibration can join the
# SAME defect across re-reviews at a granularity finer than criterion-load-delta. It is
# additive to the sidecar event ONLY — the surfaced verdict findings are untouched, so the
# library / MCP / CLI return shape does not change.
_NORM_STOP_TOKENS = 3  # drop tokens this short or shorter as low-signal noise


def norm_id(finding: dict[str, Any]) -> str:
    """A reword-tolerant, criterion-scoped content fingerprint for a finding: the SORTED
    SET of its significant lowercased alphanumeric tokens joined with its sorted criteria.
    Order-insensitive and resilient to minor re-phrasing, so the same underlying defect
    across re-reviews tends to mint the same ``norm_id`` (unlike the exact-text ``id``)."""
    text = str(finding.get("finding", "")).lower()
    tokens = sorted({t for t in re.findall(r"[a-z0-9]+", text) if len(t) > _NORM_STOP_TOKENS})
    basis = " ".join(tokens) + "|" + ",".join(sorted(finding.get("criteria", []) or []))
    return "n" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _norm_tokens(text: str) -> str:
    """The shared significant-token normalization (lowercase, alphanumeric token split,
    stop-token filtering, sorted de-duplicated join) that makes fingerprints reword-tolerant."""
    tokens = {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > _NORM_STOP_TOKENS}
    return " ".join(sorted(tokens))


def fix_unit_key(finding: dict[str, Any]) -> str:
    """A CRITERIA-FREE fix-unit fingerprint: same-location + same-claim findings share it even
    when different criteria cite them. ``norm_id`` cannot serve here — it bakes the sorted
    criteria list into its hash, so one defect co-cited by N criteria mints N distinct norm_ids.
    Location is token-set normalized (not exact-string) because independent finder criteria
    format the same location differently (path:line vs prose section names)."""
    basis = (
        _norm_tokens(str(finding.get("location", "") or ""))
        + "|"
        + _norm_tokens(str(finding.get("finding", "")))
    )
    return "g" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# ── recall: prior-review concerns re-surfaced POST-Pass-1 (story disused-unpoliced-solenodon) ──
# Verdict-flips on identical material are a RECALL problem: the fresh finder MISSES a valid finding
# a prior review caught. `prior_concerns()` returns the prior findings worth re-checking; run_pass1
# adds the ones the fresh finder missed (matched by norm_id) as post-Pass-1 candidates for the
# UNCHANGED Pass-2 verifier — the finder itself NEVER receives prior findings (independence by
# construction; the pinned test_prior_findings_only_reach_the_novelty_seam + ADR 0008 Inv. 1 hold).
RECALL_MIN_PRIORITY = 0.5  # "near/above the bar": the 0.60-0.70 blocking bars, minus a margin
RECALL_CAP = 12  # cap the recalled-candidate set to bound the added Pass-2 verification cost

# The decisions a finding was actually SURFACED to the client under (the lowercase strings
# pass3_decide emits). ``build_payload`` persists the FULL finding set — blocking + advisory +
# overflow + indeterminate + DROPPED — into ``findings`` for offline calibration, so any consumer
# that reads a prior sidecar's ``findings`` as a RE-REVIEW SIGNAL (recall re-surfacing; the
# rising-floor novelty prior set) MUST filter to surfaced-only first. Otherwise a finding
# permanently dropped for convergence re-enters the prior set and re-matches on recurrence, scoring
# LOW novelty ("carryover") and thereby ESCAPING the floor that dropped it — defeating the intended
# permanent drop (bug old-frilly-plankton). This is the single shared vocabulary so the two
# consumers can never disagree about which prior findings a re-review may reason against.
SURFACED_DECISIONS = ("block", "advisory")


def surfaced_findings(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The prior findings a re-review is allowed to reason against: those whose ``decision`` is in
    :data:`SURFACED_DECISIONS` (i.e. RETURNED TO THE CLIENT), from a ``latest_review_result``
    payload. Never the dropped/indeterminate/overflow findings ``build_payload`` also persists.

    The surfaced-only filter deliberately lives HERE (a shared helper over the reader's payload),
    not inside ``latest_review_result``: the reader's contract is the full persisted set (offline
    calibration and the remediation-eligibility existence check in ``attest.py`` read ALL findings),
    and only a re-review SIGNAL narrows to surfaced-only. Returns ``[]`` for a missing payload."""
    return [
        f for f in (result or {}).get("findings") or [] if f.get("decision") in SURFACED_DECISIONS
    ]


def prior_concerns(ticket_id: str, *, repo_root=None) -> list[dict[str, Any]]:
    """The prior-review findings worth re-checking on this ticket: from the most-recent
    REVIEW_RESULT sidecar, those that scored NEAR/ABOVE the bar — ``priority >=
    RECALL_MIN_PRIORITY`` AND ``decision`` in ``{"block", "advisory"}`` (the lowercase strings
    pass3_decide emits; excludes "dropped"/"indeterminate") — highest-priority first, capped at
    ``RECALL_CAP``.

    Best-effort and NEVER raises (mirrors the sidecar's observability posture): a missing or
    unreadable sidecar returns ``[]`` and recall becomes a no-op. Each concern carries the prior
    ``finding``/``suggested_fix``/``criteria``/``location`` + its ``norm_id`` so the caller can
    match it against the fresh findings without recomputing the fingerprint."""
    try:
        result = latest_review_result(ticket_id, repo_root=repo_root)
        if not result:
            return []
        eligible = [
            f
            for f in surfaced_findings(result)
            if float(f.get("priority") or 0.0) >= RECALL_MIN_PRIORITY
        ]
        eligible.sort(key=lambda f: float(f.get("priority") or 0.0), reverse=True)
        return [
            {
                "finding": f.get("finding", ""),
                "suggested_fix": f.get("suggested_fix", ""),
                "criteria": list(f.get("criteria", []) or []),
                "location": f.get("location", ""),
                "norm_id": f.get("norm_id") or norm_id(f),
            }
            for f in eligible[:RECALL_CAP]
        ]
    except Exception:  # noqa: BLE001 — best-effort recall reader; broad-but-logged, never fails the review
        logger.warning(
            "prior_concerns recall read failed; treating as no prior concerns", exc_info=True
        )
        return []


def review_code_sha(repo_root=None) -> str | None:
    """The review-time code SHA, by the SAME two-step rule on both the emit side (the
    ``verified_at_sha`` stamp below) and the eligibility-check side
    (``attest.remediation_mode_candidate``'s sidecar branch): the active gate-snapshot SHA
    (``current_code_sha()``, a ContextVar reader) when present, else the committed git HEAD of
    ``repo_root`` (local-mode reads have no snapshot — without this fallback the sidecar
    eligibility baseline would be structurally inert exactly in local BLOCK loops), else None
    (best-effort; never raises)."""
    try:
        from rebar.llm.config import current_code_sha

        sha = current_code_sha()
        if sha:
            return sha
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root) if repo_root else None,
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — best-effort: no resolvable SHA → None (baseline precondition fails)
        return None


def build_payload(
    verdict: dict[str, Any], *, material: str | None = None, repo_root=None
) -> dict[str, Any]:
    """The sidecar payload: per-finding fingerprints + decisions + verification
    attributes (everything needed to reconstruct per-criterion FP/remediation rates
    offline by joining on ticket_id + finding id), plus the coverage record and the
    full advisory OVERFLOW + DROPPED sets (which are not surfaced to the agent but
    are retained here for analysis)."""

    def _slim(f: dict[str, Any]) -> dict[str, Any]:
        # Field-selection principle (child e344): persist the PROSE a remediation
        # re-review's Pass-2 novelty sub-call needs to re-ground itself against the
        # prior findings (``finding`` / ``suggested_fix`` / ``checklist_item``) plus the
        # fingerprints/decision/verification needed for offline calibration. Story 4e19
        # makes the record LOSSLESS (v2): the Pass-1 ``evidence``/``scenarios`` grounding
        # prose is now persisted too, along with the resolved ``block_threshold`` /
        # ``blocking_enabled`` the Pass-3 decision applied — so an auditor can see a
        # finding's grounding quotes AND the exact decision boundary that judged it.
        # (Runtime-only carriers like ``_agentic`` are still excluded.)
        return {
            "id": f.get("id"),
            # OBSERVABILITY-ONLY enrichment (db7b follow-on): a reword-tolerant fingerprint
            # + the finding's location, so the voluntary-revision signal is cleanly joinable
            # across re-reviews offline. Not surfaced to the agent (sidecar event only).
            "norm_id": norm_id(f),
            "location": f.get("location", ""),
            "criteria": f.get("criteria", []),
            # COHORT (epic cite-stone-sea / WS9): the sorted criterion-id set co-resident in the
            # finder call that produced this finding (a singleton ["ISF"] for the ISF path). It is
            # an offline calibration carrier for R-1 (chunk-contamination analysis) — a small scalar
            # list, safe to persist here. A finding written before this field (or by a path that
            # doesn't stamp it) has NO cohort key; offline analysis MUST treat a MISSING cohort as
            # "unknown" (skip it), never as an empty set.
            "cohort": f.get("cohort"),
            "tier": f.get("tier"),
            "decision": f.get("decision"),
            "severity": f.get("severity"),
            "validity": f.get("validity"),
            "impact": f.get("impact"),
            "priority": f.get("priority"),
            "reason": f.get("reason"),
            # Which Pass-3 floor (if any) dropped this finding — the join key that disambiguates the
            # two floors offline (story 6533 / G6): "completion" (completion floor), "novelty"
            # (novelty rising floor), or null (surfaced/normal). Each floor stamps its own
            # drop_reason on the dropped finding; an un-floored finding has none.
            "drop_reason": f.get("drop_reason"),
            "verification": f.get("verification"),
            # Finding PROSE (child e344): re-grounding the Pass-2 novelty sub-call on a
            # remediation re-review needs the prior finding's actual text — not just its
            # fingerprint — to answer the matches-prior sub-answers. Sidecar event ONLY;
            # the surfaced verdict shape is byte-unchanged (asserted in tests).
            "finding": f.get("finding", ""),
            "suggested_fix": f.get("suggested_fix", ""),
            "checklist_item": f.get("checklist_item", ""),
            # Lossless v2 (story 4e19): the Pass-1 grounding prose (quotes the finder
            # cited, failure scenarios it imagined) + the resolved decision boundary the
            # Pass-3 decision applied. block_threshold/blocking_enabled ride on the finding
            # because pass3_over_findings merged the decision output (see decide.py). All
            # sidecar-event ONLY; the surfaced verdict shape is unchanged.
            "evidence": f.get("evidence", []),
            "scenarios": f.get("scenarios", []),
            "block_threshold": f.get("block_threshold"),
            "blocking_enabled": f.get("blocking_enabled"),
            # Blocking fix-unit grouping (story 5e64): the criteria-free group key + the
            # primary flag/criteria-union stamps, so offline replay can collapse one defect
            # co-cited by N criteria into one fix-unit. Absent (None) on ungrouped findings.
            "group_id": f.get("group_id"),
            "is_primary": f.get("is_primary"),
            "group_criteria": f.get("group_criteria"),
        }

    _baseline_sha = review_code_sha(repo_root)
    try:
        from .manifest import registry_version

        _baseline_regver = registry_version(repo_root)
    except Exception:  # noqa: BLE001 — best-effort baseline stamp; None fails the precondition later
        _baseline_regver = None
    all_findings = (
        verdict.get("blocking", [])
        + verdict.get("advisory", [])
        + verdict.get("overflow", [])
        + verdict.get("indeterminate", [])
        + verdict.get("dropped", [])
    )
    return {
        "schema": "plan_review_result_v2",
        "impact_model_version": IMPACT_MODEL_VERSION,
        "verdict": verdict.get("verdict"),
        "ticket_id": verdict.get("ticket_id"),
        "ticket_type": verdict.get("ticket_type"),
        "material_fingerprint": material,
        # Remediation-eligibility baseline (story a850): the review-time code SHA + registry
        # version, stamped on EVERY verdict (PASS and BLOCK alike, always self-sourced at emit)
        # so a BLOCK loop leaves a usable baseline even though a BLOCK never signs. Sourcing
        # failures stamp None (emit stays best-effort); an absent/None field simply fails the
        # sidecar-branch precondition (fail-safe).
        "verified_at_sha": _baseline_sha,
        "regver": _baseline_regver,
        "model": verdict.get("model"),
        "runner": verdict.get("runner"),
        # Per-pass latency + cost-proxy metrics (db7b AC5), lifted from coverage for
        # easy offline join (det_ms / llm_ms / total_ms / llm_calls / claim_path).
        "metrics": (verdict.get("coverage", {}) or {}).get("metrics", {}),
        "coverage": verdict.get("coverage", {}),
        "findings": [_slim(f) for f in all_findings],
        # Persist the FULL pass-4 coaching record (story a3db) — move_name, subject, and the
        # rendered coaching prose, not just {move_id, finding_refs} — so an audit UI can
        # re-render the note. Schema-tolerant: a missing field yields None, never KeyError.
        "coaching": [
            {
                "move_id": c.get("move_id"),
                "move_name": c.get("move_name"),
                "subject": c.get("subject"),
                "finding_refs": c.get("finding_refs", []),
                "coaching": c.get("coaching"),
            }
            for c in verdict.get("coaching", [])
        ],
    }
