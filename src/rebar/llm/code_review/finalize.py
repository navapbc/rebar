"""Code-review post-verdict finalization (extracted from :mod:`rebar.llm.workflow.gate_dispatch`).

This ``code_review/``-local leaf owns everything the code-review gate does *after* the
four-pass workflow produces a terminal verdict: metrics reconstruction, WS5 security
fail-closed enforcement, the content-addressed ``deps`` map, the region-gated novelty
floor, and the durable-artifact emit (incl. the LOCAL session-artifact resolve/create/
link). ``gate_dispatch._run_code_review_gate`` runs the workflow and, on success,
delegates the whole finalization tail to :func:`finalize_code_review_verdict` here,
staying a thin sequencer.

STRICT LEAF: this module imports NOTHING from ``gate_dispatch`` (that would be a
back-import). The two plan-review step ids it shares (``verify`` / ``decide``) and the
billable-tier kind set are re-declared below as small private constants that MIRROR the
canonical definitions in ``gate_dispatch`` (``STEP_VERIFY`` / ``STEP_DECIDE`` /
``_LLM_STEP_KINDS``) — a rename of those there must be mirrored here.
"""

from __future__ import annotations

from typing import Any

# Local mirrors of gate_dispatch's shared step ids/kinds, re-declared here to keep this a strict
# leaf (no back-import into gate_dispatch). These MUST track gate_dispatch.STEP_VERIFY /
# gate_dispatch.STEP_DECIDE / gate_dispatch._LLM_STEP_KINDS (identical string/set values).
_STEP_VERIFY = "verify"
_STEP_DECIDE = "decide"
_LLM_STEP_KINDS = frozenset({"agent", "batch"})  # the billable LLM tier (finders/verify/coach)

# The code-review gate's Pass-0 assemble (changed-files/diff) step id.
STEP_ASSEMBLE_DIFF = "assemble_diff"

#: High-priority floor for the approach-viability signal: a finding with kernel ``priority``
#: (validity × impact ∈ [0,1]) ≥ this is "high-priority" (keyed off priority, not severity label).
_HIGH_PRIORITY_FLOOR = 0.7


def _resolve_or_create_session_artifact(
    session_id: str, *, head: str = "HEAD", repo_root: Any = None
) -> str | None:
    """Resolve-or-create the LOCAL session-keyed ``code_review`` artifact ticket for ``session_id``
    and best-effort ``relates_to``-link the work ticket from ``head``'s ``rebar-ticket:`` trailer.
    Returns the artifact id, or ``None`` on any failure. Idempotent per session id (mirrors
    ``voter.emit_code_review_artifact``): a title match REUSES the existing artifact so two reviews
    under one session append to the SAME memory. Never raises — the artifact is best-effort, so a
    store failure must not fail the review (only local convergence memory is lost)."""
    import logging

    logger = logging.getLogger(__name__)
    try:
        import rebar

        title = f"code-review: session:{session_id}"
        artifact_id: str | None = None
        try:
            for t in rebar.list_tickets(ticket_type="code_review", repo_root=repo_root) or []:
                if str(t.get("title") or "") == title:
                    artifact_id = str(t.get("ticket_id") or t.get("id") or "") or None
                    break
        except Exception:  # noqa: BLE001 — a lookup failure just means we create a fresh artifact
            artifact_id = None
        if not artifact_id:
            created = rebar.create_ticket(
                "code_review",
                title,
                description=(
                    f"Local code-review artifact for session {session_id}. Holds the surfaced "
                    "findings + reviewed-file content-hash map that the region-gated novelty floor "
                    "converges against across `rebar review-code` runs in this session."
                ),
                return_alias=True,
                repo_root=repo_root,
            )
            artifact_id = str(created["id"] if isinstance(created, dict) else created)
        _link_session_artifact(artifact_id, head=head, repo_root=repo_root)
        return artifact_id
    except Exception:  # noqa: BLE001 — best-effort local memory; never fails the review
        logger.warning("local session code_review artifact resolve/create failed", exc_info=True)
        return None


def _link_session_artifact(artifact_id: str, *, head: str = "HEAD", repo_root: Any = None) -> None:
    """Best-effort ``relates_to`` link from the session artifact to the work ticket named in
    ``head``'s ``rebar-ticket:`` trailer (searchability). A trailerless/unresolved review still
    persists — the link is optional and never fails the review. Mirrors the voter's trailer path."""
    import logging
    import subprocess

    logger = logging.getLogger(__name__)
    try:
        import rebar
        from rebar import config as _config
        from rebar._commands.verify_commit import extract_ticket_refs
        from rebar._engine_support.resolver import resolve_ticket_id

        root = str(_config.repo_root(repo_root))
        msg = subprocess.run(
            ["git", "-C", root, "log", "-1", "--format=%B", head or "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        tracker = str(_config.tracker_dir(repo_root))
        for ref in extract_ticket_refs(msg) or []:
            resolved = resolve_ticket_id(ref, tracker)
            if resolved:
                rebar.link(artifact_id, resolved, "relates_to", repo_root=repo_root)
                return
    except Exception:  # noqa: BLE001 — the relates_to link is optional; never fails the review
        logger.warning("session artifact relates_to link skipped", exc_info=True)


def _count_diff_lines(text: str) -> int:
    """Diff-body line count: ``+``/``-`` lines, excluding the ``+++``/``---`` file headers."""
    n = 0
    for ln in text.splitlines():
        if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")):
            n += 1
    return n


def _attach_code_review_metrics(verdict: dict[str, Any], rec, total_ms: float) -> None:
    """Reconstruct ``coverage['metrics']`` from recorder step timings (code-review analog of
    ``_attach_plan_review_metrics``): llm_ms/total_ms, llm_calls, findings_per_run, verify_requests,
    and grounding_health (``"low"`` iff non-trivial diff AND 0 verifier requests). ADVISORY only
    (story 1669) — never touches ``verdict['verdict']``: emits coverage grounding_note (when
    grounding_health low) + approach_viability_note (ledger thresholds); tolerant of partials."""
    from rebar.llm.code_review.fp_ledger import (
        MAX_PASS2_DROP_RATE,
        MIN_SURVIVING_HIGH_PRIORITY,
        NON_TRIVIAL_DIFF_LINES,
        is_non_trivial_diff,
    )

    llm_ms = 0.0
    batch_criteria = 0
    agent_calls = 0
    # Pass-2 verifier model-request count (mirror plan-review's verify-step sum).
    verify_requests = 0
    # Token usage summed across every step that exposes per-call `_usage` (the runner attaches
    # it; see runner._extract_usage). Today that is the Pass-2 `verify` + Pass-3 `decide` agent
    # steps — the Pass-1 finder batch does not surface `_usage` on its step output (follow-up),
    # so these totals are the review's agent-step token usage. Enables the review bot to emit
    # token counts to CloudWatch and enriches the persisted code_review artifact.
    token_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    # Pass-3 dropped findings (from the `decide` step; absent from the terminal verdict).
    dropped = 0
    changed_files = 0
    changed_lines = 0
    for s in rec.steps:
        if not isinstance(s, dict) or s.get("status") != "succeeded":
            continue
        kind = s.get("kind")
        step_id = s.get("step_id")
        dur = s.get("duration_ms")
        outputs = s.get("outputs") or {}
        if isinstance(dur, (int, float)) and kind in _LLM_STEP_KINDS:
            llm_ms += dur
        if kind == "batch":
            batch_criteria += int(outputs.get("criteria_count") or 0)
        elif kind == "agent":
            agent_calls += 1
            if step_id == _STEP_VERIFY:
                verify_requests += int((outputs.get("_usage") or {}).get("requests") or 0)
        usage = outputs.get("_usage")
        if isinstance(usage, dict):
            for field in token_totals:
                token_totals[field] += int(usage.get(field) or 0)
        if step_id == _STEP_DECIDE:
            dropped += len(outputs.get("dropped") or [])
        if step_id == STEP_ASSEMBLE_DIFF:
            changed_files = len(outputs.get("changed_files") or [])
            changed_lines = _count_diff_lines(str(outputs.get("context") or ""))

    blocking = list(verdict.get("blocking") or [])
    # The terminal code-review verdict carries surviving advisories under `advisory` (= the
    # decide step's `surfaced`); tolerate either key.
    advisory = list(verdict.get("advisory") or verdict.get("surfaced") or [])
    surviving_high_priority = sum(
        1
        for f in advisory
        if isinstance(f, dict) and float(f.get("priority") or 0.0) >= _HIGH_PRIORITY_FLOOR
    )
    denom = dropped + len(advisory) + len(blocking)
    pass2_drop_rate = (dropped / denom) if denom else 0.0
    grounding_health = (
        "low"
        if is_non_trivial_diff(changed_files, changed_lines) and verify_requests == 0
        else "ok"
    )

    coverage = verdict.get("coverage")
    if not isinstance(coverage, dict):
        coverage = {}
        verdict["coverage"] = coverage
    coverage["llm_ran"] = True
    coverage["metrics"] = {
        "llm_ms": round(llm_ms, 1),
        "total_ms": round(total_ms, 1),
        "llm_calls": batch_criteria + agent_calls,
        "findings_per_run": len(blocking) + len(advisory),
        "verify_requests": verify_requests,
        "grounding_health": grounding_health,
        **token_totals,
        "total_tokens": token_totals["input_tokens"] + token_totals["output_tokens"],
    }
    # Advisory notes live on `coverage` (NOT in `metrics`), and NEVER on `verdict['verdict']`.
    if grounding_health == "low":
        coverage["grounding_note"] = (
            f"non-trivial diff (>{NON_TRIVIAL_DIFF_LINES} changed lines or >1 file) but the "
            "Pass-2 verifier made 0 model requests — findings may be under-grounded (advisory)."
        )
    if (
        surviving_high_priority >= MIN_SURVIVING_HIGH_PRIORITY
        or pass2_drop_rate >= MAX_PASS2_DROP_RATE
    ):
        coverage["approach_viability_note"] = (
            f"{surviving_high_priority} surviving high-priority finding(s), Pass-2 drop-rate "
            f"{pass2_drop_rate:.0%} — the approach (not just nits) may be worth a second look "
            "(advisory; the verdict is unchanged)."
        )


def finalize_code_review_verdict(
    verdict: dict[str, Any],
    *,
    request: Any,
    prep: Any,
    cfg: Any,
    runner_sel: Any,
    total_ms: float,
) -> dict[str, Any]:
    """Finalize a SUCCEEDED code-review verdict: metrics + WS5 fail-closed + content-addressed
    ``deps`` + region-gated novelty floor + durable-artifact emit (incl. the LOCAL session
    artifact). Extracted verbatim from ``gate_dispatch._run_code_review_gate``'s success branch —
    the exact ordering and best-effort try/except guards are preserved (an artifact/emit failure
    must never fail the review). ``request`` / ``prep`` are gate_dispatch's ``CodeReviewRequest`` /
    ``_CodeReviewPrep`` (typed ``Any`` to keep this a strict leaf — no back-import)."""
    _attach_code_review_metrics(verdict, prep.rec, total_ms)
    verdict.setdefault("runner", runner_sel.name)
    verdict.setdefault("model", cfg.model)
    # WS5 fail-CLOSED: a security detector abstain/match forces BLOCK (+ coverage-gap note).
    from rebar.llm.code_review import detectors as _detectors

    _detectors.apply_failclosed(
        verdict, changed_files=list(prep.dc.changed_files), repo_root=request.repo_root
    )
    # deps (story revenued-thickset-dassie): the content-addressed reviewed-file hash map the
    # region-gated novelty floor (blameless-grindable-noctule) compares against next run.
    # Computed UNCONDITIONALLY (regardless of target_ticket) and stashed on the verdict, so BOTH
    # the produce emit below AND the Gerrit voter emit (same verdict) carry it via build_payload
    # The import moves above the target_ticket check for the deps helpers. Best-effort: the
    # collector self-guards (logs + returns {}); a defensive setdefault covers any surprise.
    from rebar.llm.code_review import sidecar as _sidecar

    try:
        _dep_paths = set(prep.dc.changed_files) | _sidecar._cited_paths_code_review(verdict)
        verdict["deps"] = _sidecar.reviewed_file_hashes(_dep_paths, repo_root=request.repo_root)
    except Exception:  # noqa: BLE001 — deps collection is best-effort; never fails the gate
        verdict.setdefault("deps", {})
    # Region-gated novelty floor (story blameless-grindable-noctule): narrow the advisory set
    # against this key's prior SURFACED findings + deps BEFORE the emit, so the persisted
    # payload
    # already reflects the convergence. Keyed by the TYPED keyspace — session (local) or change
    # (Gerrit). Always active (off switch retired in story 4cdf) + self-gates inert with no
    # prior memory; any error leaves the verdict unfiltered (no drops).
    _novelty_key = None
    if request.session_id:
        _novelty_key = f"session:{request.session_id}"
    elif request.change_id:
        _novelty_key = f"change:{request.change_id}"
    if _novelty_key:
        from rebar.llm.code_review import workflow_ops as _wops

        _wops.apply_region_gated_floor(
            verdict,
            key=_novelty_key,
            cfg=cfg,
            runner=runner_sel,
            repo_root=request.repo_root,
            diff_text=prep.dc.diff_text,
        )
    # Emit the durable artifact. An explicit target_ticket (ticket-scoped review) emits
    # directly; otherwise the LOCAL session path (story paradoxal-balsamic-bubblefish)
    # resolves-or-creates a session-keyed artifact so `review-code` gains memory. Best-effort.
    target = request.target_ticket
    if not target and request.session_id:
        verdict["session_id"] = request.session_id
        target = _resolve_or_create_session_artifact(
            request.session_id, head=request.head, repo_root=request.repo_root
        )
    if target:
        _sidecar.emit(verdict, target_ticket=target, repo_root=request.repo_root)
    return verdict
