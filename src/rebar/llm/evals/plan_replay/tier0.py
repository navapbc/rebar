"""Tier-0: zero-LLM replay of Pass-3's decision math over the whole plan-review corpus
(ticket bouncy-peacockish-titmouse / 5d19-52e0-7c26-47fb).

Every input Pass-3 needs is already in the v2 sidecar (``sidecar.py`` persists each
finding's ``block_threshold``/``blocking_enabled``/``verification`` losslessly), so its
decision is exactly reproducible offline -- no LLM call, no network.

Three things are computed per finding, never conflated:

* ``stored`` -- the decision persisted in the sidecar at review time.
* ``replayed-stored`` -- :func:`replayed_stored_decision` replays :func:`pass3_decide`
  fed the finding's PERSISTED threshold/posture (no re-resolution). A pure harness
  self-check: this MUST always equal ``stored``, independent of any registry drift,
  because it uses the exact numbers the finding was originally judged against.
* ``live-baseline`` -- :func:`live_baseline_decisions` calls
  ``orchestrator.pass3_over_findings`` AS-IS (current production, live-resolved
  thresholds). This MAY legitimately differ from ``stored`` when the criteria
  registry's thresholds (governed by ``regver``) moved since the review ran --
  registry drift, not a harness bug.

A **candidate** (:mod:`.candidates`) is diffed against ``live-baseline`` --
:func:`candidate_decisions` mirrors ``orchestrator.pass3_over_findings``'s private
``_threshold_for`` closure (the ``prerequisite-consistency`` special case, the
``execution_review`` threshold bump) and its on-target-veto cohort restriction
(``_restrict_on_target_veto_to_grounded``) identically, but resolves per-criterion
thresholds against the candidate's overlay-merged registry instead of the live one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rebar.llm import criteria as _criteria
from rebar.llm.evals.plan_replay import corpus
from rebar.llm.evals.plan_replay.candidates import CANDIDATES, Candidate
from rebar.llm.plan_review import orchestrator, registry, sidecar
from rebar.llm.review_kernel import decide


def execution_review_for(data: dict[str, Any]) -> bool:
    """Recover a sidecar row's ``execution_review`` flag from its ``review_phase``
    metadata (``sidecar.parse_review_phase_metadata``), defaulting to planning (False)
    on any malformed payload -- ``execution_review`` is NOT a persisted per-finding
    field, so it must be recovered once per row and threaded into every Pass-3 call."""
    try:
        phase_metadata = sidecar.parse_review_phase_metadata(data)
    except sidecar.SidecarReviewPhaseError:
        return False
    return phase_metadata["phase"] == "execution"


def _effective_routing_map(overlay: dict[str, tuple[float, bool]]) -> dict[str, dict[str, Any]]:
    """The live criteria registry map with ``overlay`` entries substituted in, in the
    ``{block_threshold, default_posture}`` shape ``_criteria.threshold_for`` reads --
    an unmapped criterion falls through to its live registry entry unchanged."""
    crit_by_id = registry.by_id()
    merged = dict(crit_by_id)
    for criterion_id, (block_threshold, blocking) in overlay.items():
        merged[criterion_id] = {
            **(crit_by_id.get(criterion_id) or {}),
            "block_threshold": block_threshold,
            "default_posture": "blocking" if blocking else "advisory",
        }
    return merged


def mirrored_threshold_for(
    candidate: Candidate, *, execution_review: bool
) -> decide.ThresholdResolver:
    """A ``threshold_for`` closure mirroring ``orchestrator.pass3_over_findings``'s
    private ``_threshold_for`` exactly (the ``prerequisite-consistency`` special case,
    the ``execution_review`` bump to ``max(bt, 0.80)``), resolved against
    ``candidate``'s overlay-merged registry instead of the live one."""
    routing_map = _effective_routing_map(candidate.overlay)

    def _threshold_for(criteria: Any) -> tuple[float, bool]:
        if "prerequisite-consistency" in set(criteria or []):
            return 0.60, True
        block_threshold, blocking_enabled = _criteria.threshold_for(
            criteria, routing_map, gate="plan_review"
        )
        if execution_review and blocking_enabled:
            block_threshold = max(block_threshold, 0.80)
        return block_threshold, blocking_enabled

    return _threshold_for


def replayed_stored_decision(finding: dict[str, Any], *, execution_review: bool) -> dict[str, Any]:
    """Replay one finding's decision from its PERSISTED ``block_threshold``/
    ``blocking_enabled`` (no threshold re-resolution) -- the harness self-check that
    must always equal ``stored``, independent of registry drift."""
    return decide.pass3_decide(
        finding.get("verification"),
        block_threshold=float(finding.get("block_threshold") or decide.DEFAULT_BLOCK_THRESHOLD),
        blocking_enabled=bool(finding.get("blocking_enabled")),
        impact_fn=decide.impact_plan,
        execution_review=execution_review,
    )


def live_baseline_decisions(
    findings: list[dict[str, Any]],
    verifs: dict[int, dict[str, Any]],
    *,
    execution_review: bool,
) -> list[dict[str, Any]]:
    """Current production behavior for this row, called AS-IS -- byte-identical to what
    the live plan-review gate would decide today."""
    return orchestrator.pass3_over_findings(findings, verifs, execution_review=execution_review)


def candidate_decisions(
    findings: list[dict[str, Any]],
    verifs: dict[int, dict[str, Any]],
    candidate: Candidate,
    *,
    execution_review: bool,
) -> list[dict[str, Any]]:
    """Replay ``findings`` under ``candidate``'s overlay/impact_fn, mirroring the
    plan-review wrapper's on-target-veto cohort restriction identically."""
    threshold_for = mirrored_threshold_for(candidate, execution_review=execution_review)
    if execution_review:
        verifs = orchestrator._restrict_on_target_veto_to_grounded(findings, verifs)
    return decide.pass3_over_findings(
        findings,
        verifs,
        threshold_for=threshold_for,
        impact_fn=candidate.impact_fn,
        execution_review=execution_review,
    )


def sidecar_data_for_row(
    row: dict[str, Any],
    event_index: dict[tuple[str, str], corpus._Blob],
    tracker_paths: dict[str, str],
) -> dict[str, Any] | None:
    """Best-effort full-body re-read of a corpus row's REVIEW_RESULT sidecar payload --
    the corpus cache row is summary-only, so ``findings``/``verification``/
    ``review_phase`` need the same git-object-walk re-read :mod:`labels` uses, matched
    by ``(ticket_id, review_event_uuid)``. Returns ``None`` on any unmatched or
    unreadable body rather than raising."""
    key = (row["ticket_id"], row["review_event_uuid"])
    blob = event_index.get(key)
    if blob is None:
        return None
    tracker_path = tracker_paths.get(row["store"])
    if tracker_path is None:
        return None
    bodies = corpus._batch_cat(tracker_path, [blob.sha])
    raw = bodies.get(blob.sha)
    if raw is None:
        return None
    try:
        data = json.loads(raw).get("data", {})
    except (json.JSONDecodeError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


def build_event_index(store_roots: dict[str, str]) -> dict[tuple[str, str], corpus._Blob]:
    """``{(ticket_id, review_event_uuid): blob}`` over every REVIEW_RESULT blob in every
    store -- shared by every row's :func:`sidecar_data_for_row` call."""
    index: dict[tuple[str, str], corpus._Blob] = {}
    for tracker_path in store_roots.values():
        for blob in corpus._enumerate_event_blobs(tracker_path):
            if blob.kind == "REVIEW_RESULT":
                index[(blob.ticket_id, blob.uuid)] = blob
    return index


def verifs_from_findings(findings: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """The ``{index: verification}`` map Pass-3 consumes, built from a sidecar's
    persisted per-finding ``verification`` field."""
    result: dict[int, dict[str, Any]] = {}
    for i, f in enumerate(findings):
        verification = f.get("verification")
        if verification:
            result[i] = verification
    return result


def replay_row(
    row: dict[str, Any],
    data: dict[str, Any],
    candidate: Candidate,
) -> dict[str, Any] | None:
    """Replay one corpus row's whole finding set through all three lenses (``stored``,
    ``replayed-stored``, ``live-baseline``) plus ``candidate``. Returns ``None`` when
    the sidecar body carries no usable ``block_threshold`` (a v1 sidecar, pre-lossless
    persistence) -- such rows are skipped and counted by the caller, not replayed."""
    findings = data.get("findings")
    if not isinstance(findings, list) or not findings:
        return None
    if not any(isinstance(f.get("block_threshold"), (int, float)) for f in findings):
        return None

    execution_review = execution_review_for(data)
    verifs = verifs_from_findings(findings)

    replayed_stored = [
        replayed_stored_decision(f, execution_review=execution_review) for f in findings
    ]
    live_baseline = live_baseline_decisions(findings, verifs, execution_review=execution_review)
    candidate_result = candidate_decisions(
        findings, verifs, candidate, execution_review=execution_review
    )

    return {
        "ticket_id": row["ticket_id"],
        "review_event_uuid": row["review_event_uuid"],
        "execution_review": execution_review,
        "stored": [f.get("decision") for f in findings],
        "replayed_stored": [d["decision"] for d in replayed_stored],
        "live_baseline": [d["decision"] for d in live_baseline],
        "candidate": [d["decision"] for d in candidate_result],
    }


_BLOCKING = "block"


def flip_matrix(replayed_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate flip counts across every replayed row: ``self_check_mismatches``
    (``replayed_stored`` vs ``stored`` -- must be zero), ``registry_drift_flips``
    (``stored`` vs ``live_baseline``), and ``candidate_flips``/``friction_rate``/
    ``relief_count`` (``live_baseline`` vs ``candidate`` -- the primary signal)."""
    self_check_mismatches = 0
    registry_drift_flips = 0
    candidate_newly_blocking = 0
    candidate_relief = 0
    total_findings = 0

    for row in replayed_rows:
        for stored, replayed, live, cand in zip(
            row["stored"],
            row["replayed_stored"],
            row["live_baseline"],
            row["candidate"],
            strict=True,
        ):
            total_findings += 1
            if replayed != stored:
                self_check_mismatches += 1
            if live != stored:
                registry_drift_flips += 1
            live_blocking = live == _BLOCKING
            cand_blocking = cand == _BLOCKING
            if not live_blocking and cand_blocking:
                candidate_newly_blocking += 1
            if live_blocking and not cand_blocking:
                candidate_relief += 1

    friction_rate = candidate_newly_blocking / total_findings if total_findings else 0.0

    return {
        "total_findings": total_findings,
        "self_check_mismatches": self_check_mismatches,
        "registry_drift_flips": registry_drift_flips,
        "candidate_newly_blocking": candidate_newly_blocking,
        "friction_rate": friction_rate,
        "relief_count": candidate_relief,
    }


def ticket_label_from_labels_row(ticket_labels: dict[str, Any]) -> bool | None:
    """``True`` (escaped), ``False`` (clean), or ``None`` (no usable label yet) for one
    ticket's :func:`rebar.llm.evals.plan_replay.labels`-derived ``ticket_labels`` dict."""
    from rebar.llm.evals.plan_replay import labels as labels_mod

    escape = labels_mod.escape_signals(
        escaped=bool(ticket_labels.get("escaped_defect")),
        completion_failed=bool(ticket_labels.get("completion_failed_after_pass")),
        reopened=bool(ticket_labels.get("reopened")),
        forced=bool(ticket_labels.get("force_close")),
    )
    if escape:
        return True
    if ticket_labels.get("clean_close"):
        return False
    return None


def label_proxy_metrics(
    replayed_rows: list[dict[str, Any]], ticket_labels_by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """``blocking_agreement_rate``/``proxy_precision``/``proxy_recall``/
    ``coverage_fraction`` over the tickets with a usable label: a ticket's candidate
    verdict is BLOCK iff any of its replayed findings decided ``"block"``; the label is
    escape (positive) / clean (negative) via :func:`ticket_label_from_labels_row`."""
    by_ticket_candidate_block: dict[str, bool] = {}
    for row in replayed_rows:
        blocked = any(d == _BLOCKING for d in row["candidate"])
        tid = row["ticket_id"]
        by_ticket_candidate_block[tid] = by_ticket_candidate_block.get(tid, False) or blocked

    tp = fp = tn = fn = 0
    usable = 0
    for ticket_id, candidate_blocks in by_ticket_candidate_block.items():
        tlabels = ticket_labels_by_id.get(ticket_id)
        if tlabels is None:
            continue
        label = ticket_label_from_labels_row(tlabels)
        if label is None:
            continue
        usable += 1
        if candidate_blocks and label:
            tp += 1
        elif candidate_blocks and not label:
            fp += 1
        elif not candidate_blocks and label:
            fn += 1
        else:
            tn += 1

    total_tickets = len(by_ticket_candidate_block)
    coverage_fraction = usable / total_tickets if total_tickets else 0.0

    return {
        "blocking_agreement_rate": (tp + tn) / usable if usable else None,
        "proxy_precision": tp / (tp + fp) if (tp + fp) else None,
        "proxy_recall": tp / (tp + fn) if (tp + fn) else None,
        "coverage_fraction": coverage_fraction,
    }


def _load_cache_rows(cache_dir: Path, content_hash: str) -> list[dict[str, Any]]:
    cache_path = cache_dir / f"{content_hash}.jsonl"
    rows: list[dict[str, Any]] = []
    with cache_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def run_tier0(
    store_roots: dict[str, str],
    *,
    cache_dir: Path | str,
    candidate_name: str,
    labels_path: str | None = None,
) -> dict[str, Any]:
    """The Tier-0 integration driver: builds/reuses the verified corpus, replays every
    row's Pass-3 decision under ``candidate_name``, and returns the aggregate report
    payload (``report.render_report`` turns this into Markdown)."""
    if candidate_name not in CANDIDATES:
        raise KeyError(
            f"unknown candidate {candidate_name!r}; known candidates: {sorted(CANDIDATES)}"
        )
    candidate = CANDIDATES[candidate_name]

    manifest = corpus.build_corpus(store_roots, cache_dir=cache_dir)
    rows = _load_cache_rows(Path(cache_dir), manifest["content_hash"])
    event_index = build_event_index(store_roots)

    replayed_rows: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        data = sidecar_data_for_row(row, event_index, store_roots)
        if data is None:
            skipped += 1
            continue
        replayed = replay_row(row, data, candidate)
        if replayed is None:
            skipped += 1
            continue
        replayed_rows.append(replayed)

    label_proxy = None
    if labels_path is not None:
        from rebar.llm.evals.plan_replay import labels as labels_mod

        label_rows = labels_mod.load_labels(
            labels_path, store_roots=store_roots, cache_dir=cache_dir
        )
        ticket_labels_by_id: dict[str, dict[str, Any]] = {}
        for lr in label_rows:
            tid = lr.get("ticket_id")
            tl = lr.get("ticket_labels")
            if tid and tl:
                ticket_labels_by_id[tid] = tl
        label_proxy = label_proxy_metrics(replayed_rows, ticket_labels_by_id)

    return {
        "content_hash": manifest["content_hash"],
        "candidate": candidate_name,
        "row_count": len(replayed_rows),
        "skipped": skipped,
        "flip_matrix": flip_matrix(replayed_rows),
        "label_proxy_metrics": label_proxy,
    }
