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

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Local mirrors of gate_dispatch's shared step ids/kinds, re-declared here to keep this a strict
# leaf (no back-import into gate_dispatch). These MUST track gate_dispatch.STEP_VERIFY /
# gate_dispatch.STEP_DECIDE / gate_dispatch._LLM_STEP_KINDS (identical string/set values).
_STEP_VERIFY = "verify"
_STEP_DECIDE = "decide"
_LLM_STEP_KINDS = frozenset({"agent", "batch"})  # the billable LLM tier (finders/verify/coach)

# The code-review gate's Pass-0 assemble (changed-files/diff) step id.
STEP_ASSEMBLE_DIFF = "assemble_diff"

#: Every code-review step id this module looks up BY NAME. DERIVED from the constants
#: above rather than re-typed, so it cannot become yet another copy of the same strings
#: (mirror F13). The dispatcher validates the loaded gate YAML against this: a rename in
#: gates/code-review.yaml would otherwise make these lookups silently return None and
#: degrade a recoverable run, exactly as the plan-review gate's own validator prevents.
CODE_REVIEW_STEP_IDS = frozenset({_STEP_VERIFY, _STEP_DECIDE, STEP_ASSEMBLE_DIFF})

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
    except Exception:
        logger.warning("local session code_review artifact resolve/create failed", exc_info=True)
        return None


def _link_session_artifact(artifact_id: str, *, head: str = "HEAD", repo_root: Any = None) -> None:
    """Best-effort ``relates_to`` link from the session artifact to the work ticket named in
    ``head``'s ``rebar-ticket:`` trailer (searchability). A trailerless/unresolved review still
    persists — the link is optional and never fails the review. Mirrors the voter's trailer path."""
    import subprocess

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
    except Exception:
        logger.warning("session artifact relates_to link skipped", exc_info=True)


def _count_diff_lines(text: str) -> int:
    """Diff-body line count: ``+``/``-`` lines, excluding the ``+++``/``---`` file headers."""
    n = 0
    for ln in text.splitlines():
        if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")):
            n += 1
    return n


def _expects_verification(f: Any) -> bool:
    """Is this finding one that SHOULD carry a Pass-2 ``verification`` block?

    Only LLM-tier DECIDED findings are. Two populations are legitimately blockless and must be
    excluded, or the missing-recording signal below becomes a standing false alarm rather than an
    alert (they are ~2.5% of the recorded corpus, present on routine runs):

    * ``tier != "LLM"`` — deterministic findings (the WS5 fail-closed security detectors, the
      bugfix-size gate) are injected AFTER Pass-3 and never enter Pass-2, so no verification for
      them was ever produced to record. An untiered finding is treated the same way.
    * ``decision == "indeterminate"`` / ``reason == "no-verification"`` — the verifier emitted
      nothing for this index and ``pass3_decide`` degraded the finding on purpose. That path
      already has its own signal (the decision itself); it is not a recording failure.
    """
    if not isinstance(f, dict):
        return False
    if f.get("tier") != "LLM":
        return False
    return f.get("decision") != "indeterminate" and f.get("reason") != "no-verification"


def _verification_recording(verdict: dict[str, Any], *, verify_requests: int) -> tuple[bool, int]:
    """``(recorded, missing_count)`` for the Pass-2 verification blocks on ``verdict``'s findings.

    The metrics gap this closes (bug abdominal-grieving-nandu / operator escalation R3): the
    sidecar persists each finding's ``verification`` by FIELD-SPREAD, so a regression that stopped
    producing or carrying the block would silently write ~1,400 reviews' worth of unverified
    findings with nothing erroring or warning. ``recorded`` is False ONLY when Pass-2 demonstrably
    ran (``verify_requests > 0``) and an expected block is nonetheless absent — so a review that
    never reached the verifier reports True here and is flagged by ``grounding_health`` instead,
    keeping the two signals from double-counting one outage."""
    # `advisory` / `surfaced` are the SAME bucket under two key spellings (the terminal verdict vs
    # the decide step's output); take one, never both, so a verdict carrying both cannot
    # double-count its advisories into the warning's missing/expected tally.
    buckets = [
        verdict.get("blocking") or [],
        verdict.get("advisory") or verdict.get("surfaced") or [],
        verdict.get("dropped") or [],
        verdict.get("indeterminate") or [],
    ]
    expected = [f for bucket in buckets for f in bucket if _expects_verification(f)]
    missing = sum(1 for f in expected if not f.get("verification"))
    return (not (verify_requests > 0 and missing)), missing


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
    # Token usage summed across every step that exposes per-call `_usage`: the Pass-2 `verify` +
    # Pass-3 `decide` agent steps (the runner attaches it; see runner._extract_usage) and the
    # Pass-1 finder batch, which aggregates its per-overlay calls into the same flat token shape
    # (CodeReviewBatchRunner, task 514d). So these totals are the review's WHOLE token usage.
    # Enables the review bot to emit token counts to CloudWatch and enriches the persisted
    # code_review artifact.
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
    verification_recorded, missing_verifications = _verification_recording(
        verdict, verify_requests=verify_requests
    )
    coverage["llm_ran"] = True
    coverage["metrics"] = {
        "llm_ms": round(llm_ms, 1),
        "total_ms": round(total_ms, 1),
        "llm_calls": batch_criteria + agent_calls,
        "findings_per_run": len(blocking) + len(advisory),
        "verify_requests": verify_requests,
        "grounding_health": grounding_health,
        # The missing-recording signal (bug abdominal-grieving-nandu): False means Pass-2 ran but
        # an LLM-tier decided finding reached the sidecar with no verification block. Always
        # present, so "no absence detected" and "the check never ran" are distinguishable offline
        # — the whole point is that an ABSENCE is now measurable instead of silent.
        "verification_recorded": verification_recorded,
        **token_totals,
        "total_tokens": token_totals["input_tokens"] + token_totals["output_tokens"],
    }
    if not verification_recorded:
        logger.warning(
            "code-review Pass-2 verification recording gap: %d LLM-tier finding(s) reached the "
            "verdict with no verification block despite %d verifier request(s) "
            "(coverage.metrics.verification_recorded=false; the verdict is unchanged). The "
            "offline calibration corpus is incomplete for this run.",
            missing_verifications,
            verify_requests,
        )
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


def _verify_step_provenance(rec) -> dict[str, Any] | None:
    """The provenance record the runner stamped onto the Pass-2 ``verify`` agent step's outputs,
    read back off the recorder — the SAME record ``code-review.yaml`` wires into the verdict step,
    NOT a fresh resolution (``capabilities.provenance_for`` forbids recomputing it: a second
    resolution can diverge from the endpoint/caps that served the run). Mirrors
    ``workflow/plan_review_recovery.py``'s read of the verify step's outputs.

    ``None`` when no record is there — no provider resolved, or the doc in play has no ``verify``
    agent step. Absence must stay absence; a cfg-derived stand-in would make the verdict claim a
    provider served it when none did."""
    for s in getattr(rec, "steps", None) or []:
        if not isinstance(s, dict) or s.get("step_id") != _STEP_VERIFY:
            continue
        record = (s.get("outputs") or {}).get("provider_provenance")
        if isinstance(record, dict):
            return record
    return None


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
    # `model` above is CONFIGURED INTENT (cfg.model). What actually served the run is the runner's
    # provenance record, which `code-review.yaml` wires from the Pass-2 verify agent step into the
    # verdict; this backstop recovers the same record straight off the recorder for a verdict that
    # arrived without it (e.g. a gate doc that omits the wire), so the signed sidecar never records
    # a bare model string alone. Only ever the OBSERVED record — absent stays absent (task e951).
    observed_provenance = verdict.get("provider_provenance") or _verify_step_provenance(prep.rec)
    if observed_provenance is not None:
        verdict["provider_provenance"] = observed_provenance
    # WS5 fail-CLOSED: a security detector abstain/match forces BLOCK (+ coverage-gap note).
    from rebar.llm.code_review import detectors as _detectors

    _detectors.apply_failclosed(
        verdict, changed_files=list(prep.dc.changed_files), repo_root=request.repo_root
    )
    # Bugfix-size attestation criterion (ticket ad0d B2): a Gerrit bug-fix change over the
    # non-test size floor must carry a valid plan-review attestation on its trailer ticket.
    # Gerrit-only (change_id) — a local `review-code` preview never blocks on it; the gate
    # itself never raises (infra trouble abstains: the verdict becomes INDETERMINATE, bug 9011).
    if request.change_id:
        from rebar.llm.code_review import bugfix_size_gate as _bugfix_size

        _bugfix_size.apply_bugfix_size_gate(
            verdict,
            diff_text=prep.dc.diff_text,
            commit_message=getattr(request, "commit_message", "") or "",
            repo_root=request.repo_root,
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
        usage = verdict.get("_usage") if isinstance(verdict.get("_usage"), dict) else {}
        for fetch in usage.get("distinct_fetches", []) if isinstance(usage, dict) else []:
            target = fetch.get("target") if isinstance(fetch, dict) else None
            if (
                isinstance(target, str)
                and "*" not in target
                and target.endswith((".tf", ".tf.json"))
            ):
                _dep_paths.add(target)
        verdict["deps"] = _sidecar.reviewed_file_hashes(_dep_paths, repo_root=request.repo_root)
    except Exception:  # noqa: BLE001 — deps collection is best-effort; never fails the gate
        verdict.setdefault("deps", {})
    # Region-gated novelty floor (story blameless-grindable-noctule): narrow the advisory set
    # against this key's prior SURFACED findings + deps BEFORE the emit, so the persisted
    # payload
    # already reflects the convergence. Keyed by the TYPED keyspace — session (local) or change
    # (Gerrit). Always active (off switch retired in story 4cdf) + self-gates inert with no
    # prior memory; any error leaves the verdict unfiltered (no drops).
    _novelty_key = _sidecar.memory_key(request.session_id, request.change_id)
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
