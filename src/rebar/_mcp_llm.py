"""LLM-tool registrar for the rebar MCP server.

``register_llm_tools(mcp, ctx)`` registers the ``REBAR_MCP_ALLOW_LLM``-gated agent
tools (review_code, scan_spec, verify_completion, review_plan). Split
out of ``rebar.mcp_server.build_server`` as a pure structural refactor — names,
signatures, docstrings, and gating are behaviour-identical to the in-line originals.

The tools are always REGISTERED (so they appear in ``list_tools()``); each guards at
CALL time on ``_allow_llm`` — a live, billable LLM call is refused with a clear error
unless enabled. ``review_plan`` additionally reads ``_readonly`` to decide whether to
sign/emit the sidecar. Both helpers are captured off ``ctx`` and rebound to their
original local names so the tool bodies are copied verbatim. Every tool returns a
plain ``dict`` (a model-produced result), so no output models are imported here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from rebar._mcp_inflight import GateJobHandle, begin_gate_job, run_gate_singleflight
from rebar._mcp_models import tool_annotation_presets

logger = logging.getLogger(__name__)


def _structured_llm_failure(exc: Exception) -> dict:
    """Convert a raised ``LLMError`` into a STRUCTURED MCP tool RESULT (story
    authorial-hated-blackbear) rather than letting it propagate as an opaque FastMCP tool
    error. The driving agent can then branch on ``retryable`` (retry vs. escalate) instead of
    string-parsing an error. Carries the classifier disposition (``resolution_class`` /
    ``diagnostic``) when the raised error had one attached (mamba's run seam / preflight).

    The ``error`` code is derived from the shared ``error_code_for`` classifier so this second
    LLM-tier failure site honours the same taxonomy as the generic MCP guard (bug
    dbca-97ac-ad96-4d6d): a genuine outage stays ``llm_unavailable``, while a workflow
    caller-input / not-found error carried on an ``LLMError`` subtype gets its precise code."""
    from rebar._errors import error_code_for
    from rebar.llm.failure import outcome_of

    o = outcome_of(exc)
    return {
        "error": error_code_for(exc),
        "message": str(exc),
        "resolution_class": o.resolution_class.value if o is not None else None,
        "retryable": bool(o.retryable) if o is not None else False,
        "diagnostic": o.diagnostic if o is not None else None,
    }


def _with_attestation(result, classify) -> dict:
    """Attach the shared passed-but-unsigned classification to a ``review_plan`` result.

    A PASS whose attestation failed to persist is NOT a success — the signature the claim
    gate consumes was never written — but MCP has no exit code to say so the way the CLI's
    exit 11 does. The classifier lives below both surfaces
    (``rebar.llm.plan_review.resign``); this rides its verdict on the payload as
    ``attestation`` so a driving agent branches on ``retryable``/``recovery_tool`` rather
    than parsing English (ticket ammonic-amoral-nabarlek)."""
    if isinstance(result, dict):
        result["attestation"] = classify(result).as_dict()
    return result


def _record_verify_completion(result, ticket_id: str, *, readonly: bool):
    """Record a standalone MCP ``verify_completion`` run on the ticket — or NOTHING in
    read-only mode. Symmetric with ``review_plan``'s read-only behaviour.

    A read-only server must not mutate the store, so recording (which emits the
    ``COMPLETION_VERDICT`` sidecar, itself an ``append_event``) is SKIPPED ENTIRELY rather than
    merely left unsigned — a ``READ_ONLY_OPEN_WORLD`` tool performs no writes. A writable server
    records via the shared producer: an attested PASS signs the reusable attestation and the
    sidecar captures PASS/FAIL. The recording outcome rides on the result's ``record`` field."""
    if not isinstance(result, dict):
        return result
    if readonly:
        result["record"] = {
            "signed": False,
            "cause": "read_only",
            "sidecar_written": False,
            "error": "",
        }
        return result
    from rebar._commands.transition_close import record_completion_verdict

    result["record"] = record_completion_verdict(result, ticket_id)
    return result


def _review_plan_body(ticket_id: str, ref, source, force: bool, *, readonly: bool) -> dict:
    """The complete synchronous ``review_plan`` computation — the unit the singleflight
    de-duplicates. Behaviour is identical to the former in-line tool body: run the
    gate, convert an ``LLMError`` to a structured result, then attach the
    passed-but-unsigned classification. Extracted to module level so the sync tool body
    can hand this whole closure to :func:`run_gate_singleflight` and every attached
    caller shares ONE run's verdict (and ONE signature/sidecar)."""
    import rebar.llm
    from rebar.llm.plan_review.resign import classify_plan_review_attestation

    try:
        result = rebar.llm.review_plan(
            ticket_id,
            ref=ref,
            source=source,
            sign=not readonly,
            emit_sidecar=not readonly,
            force=force,
        )
    except rebar.llm.LLMError as exc:
        return _structured_llm_failure(exc)
    # The CLI maps this same classification to exit 11; MCP has no exit code, so the
    # structured verdict rides on the payload instead (ticket ammonic-amoral-nabarlek).
    return _with_attestation(result, classify_plan_review_attestation)


def _verify_completion_body(ticket_id: str, graph, ref, source, *, readonly: bool) -> dict:
    """The complete synchronous ``verify_completion`` computation — the unit the
    singleflight de-duplicates. Identical to the former in-line body: run the gate,
    convert an ``LLMError`` to a structured result, then record the run (or NOTHING in
    read-only mode). One deduped run => one recording, which is exactly the intent."""
    import rebar.llm

    try:
        result = rebar.llm.verify_completion(ticket_id, graph=graph, ref=ref, source=source)
    except rebar.llm.LLMError as exc:
        return _structured_llm_failure(exc)
    return _record_verify_completion(result, ticket_id, readonly=readonly)


def _terminal_from_result(result: Any) -> tuple[str, Any]:
    """Classify a completed gate ``result`` into a (status, verdict) pair for the run index.

    A structured LLM-failure dict (an ``error`` key) settled the run without a verdict =>
    ``failed``; any other completion => ``passed`` carrying the gate's own PASS/BLOCK
    ``verdict`` (a BLOCK is a run that COMPLETED, not one that errored)."""
    if isinstance(result, dict) and result.get("error"):
        return "failed", result.get("error")
    verdict = result.get("verdict") if isinstance(result, dict) else None
    return "passed", verdict


def _spawn_gate_daemon(
    handle: GateJobHandle, gate_type: str, ticket_id: str, work: Callable[[], Any]
) -> None:
    """Run ``work`` on a background **daemon thread** (mirrors ``run_workflow``), recording
    a terminal status to the durable ``.rebar/gate_runs`` index in a ``finally`` so a
    poller always settles — even if the gate raises before producing a verdict — and
    releasing any singleflight followers via ``handle.complete``.

    DURABILITY IS LIMITED, exactly like ``run_workflow``: the daemon does not survive the
    MCP process exiting and there is no reaper, so a process death mid-run leaves the index
    at ``running`` — which ``gate_status`` surfaces as ``stale-running`` (the gate's own
    attestation, read via the ``durable`` field, remains the authoritative verdict)."""
    import rebar.llm

    def _bg() -> None:
        result: Any = None
        error: BaseException | None = None
        status, verdict = "failed", None
        try:
            result = work()
            status, verdict = _terminal_from_result(result)
        except BaseException as exc:  # noqa: BLE001 — reflected in the run index, not raised
            error, verdict = exc, str(exc)
        finally:
            rebar.llm.record_gate_run(
                {
                    "job_id": handle.job_id,
                    "ticket_id": ticket_id,
                    "gate_type": gate_type,
                    "status": status,
                    "verdict": verdict,
                    "error": str(error) if error is not None else None,
                    "finished_at": time.time(),
                }
            )
            handle.complete(result=result, error=error)

    threading.Thread(target=_bg, daemon=True).start()


def _start_gate_job(
    gate_type: str,
    ticket_id: str,
    *,
    ref: str | None,
    source: str | None,
    variant: str,
    readonly: bool,
    force: bool,
    work: Callable[[], Any],
) -> dict:
    """Reserve a singleflight slot, record a ``running`` handle, and (only as the leader)
    spawn the background gate. Returns ``{job_id, ticket_id, gate_type, status:"running"}``
    in milliseconds. A concurrent ``*_start`` for the same key ATTACHES — it shares the
    in-flight ``job_id`` and does NOT launch a second billable run (bug d80d Phase 2)."""
    import rebar.llm

    handle = begin_gate_job(
        gate_type,
        ticket_id,
        ref=ref,
        source=source,
        variant=variant,
        readonly=readonly,
        force=force,
    )
    rebar.llm.record_gate_run(
        {
            "job_id": handle.job_id,
            "ticket_id": ticket_id,
            "gate_type": gate_type,
            "status": "running",
            "started_at": time.time(),
        }
    )
    if handle.is_new:
        _spawn_gate_daemon(handle, gate_type, ticket_id, work)
    return {
        "job_id": handle.job_id,
        "ticket_id": ticket_id,
        "gate_type": gate_type,
        "status": "running",
    }


def _register_gate_start_tools(mcp, ann, allow_llm, readonly) -> None:
    """Register the async ``*_start`` gate tools (bug d80d Phase 2).

    A module-level registrar rather than two more nested ``def``\\s inside
    ``register_llm_tools``: each nested tool costs that already-near-ceiling function a
    McCabe point (the shrink-only complexity ratchet caps it), so the Phase-2 pair lives
    here — the same factoring ``_mcp_reads`` uses for ``_register_plan_review_tools``."""

    @mcp.tool(annotations=ann["READ_ONLY_OPEN_WORLD"])
    async def review_plan_start(
        ticket_id: str,
        ref: str | None = None,
        source: str | None = None,
        force: bool = False,
    ) -> dict:
        """Start the plan-review gate ASYNC; returns {job_id, ticket_id, gate_type,
        status:'running'} IMMEDIATELY (in ms) — the review runs on a background daemon
        thread, so it OUTLIVES the client's request deadline. This is the timeout-proof
        way to run the gate: unlike the sync ``review_plan`` (which the ~60s client
        deadline can cut off with a ``-32001`` while the server keeps running), the
        caller gets a durable handle instead of a timeout, then POLLS —
        ``plan_review_status(ticket_id)`` for the durable attestation verdict, or
        ``gate_status(job_id)`` for the run handle (running -> passed/failed). PREFER
        this for a long review; the sync ``review_plan`` remains the dedup-protected
        fallback.

        A duplicate ``review_plan_start`` for the same ticket+basis while a run is in
        flight ATTACHES to it — same ``job_id``, no second billable pass (bug d80d).
        ``force=True`` starts a fresh run (bypasses de-dup). The verdict persists to the
        durable event log (the signed attestation); the ``.rebar/gate_runs`` index is a
        local handle only. DURABILITY IS LIMITED like ``run_workflow``: the daemon does
        not survive the process exiting and there is no reaper.

        DISABLED unless REBAR_MCP_ALLOW_LLM=1 (it makes live, billable LLM calls)."""
        if not allow_llm():
            raise ValueError(
                "review_plan_start is disabled: it makes live, billable LLM calls. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        ro = readonly()
        return _start_gate_job(
            "plan_review",
            ticket_id,
            ref=ref,
            source=source,
            variant=f"source={source or 'attested'}",
            readonly=ro,
            force=force,
            work=lambda: _review_plan_body(ticket_id, ref, source, force, readonly=ro),
        )

    @mcp.tool(annotations=ann["READ_ONLY_OPEN_WORLD"])
    async def verify_completion_start(
        ticket_id: str,
        graph: bool = False,
        ref: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Start the completion-verifier gate ASYNC; returns {job_id, ticket_id,
        gate_type, status:'running'} IMMEDIATELY (in ms) — the verification runs on a
        background daemon thread, so it OUTLIVES the client's request deadline. The
        timeout-proof way to run the close gate: the caller gets a durable handle
        instead of the ``-32001`` the sync ``verify_completion`` risks, then POLLS —
        ``verify_completion_status(ticket_id)`` for the durable attestation verdict, or
        ``gate_status(job_id)`` for the run handle (running -> passed/failed). PREFER
        this for a long verification; sync ``verify_completion`` is the dedup-protected
        fallback.

        A duplicate ``verify_completion_start`` for the same ticket+basis while a run is
        in flight ATTACHES to it — same ``job_id``, no second billable pass (bug d80d).
        The verdict persists to the durable event log; the ``.rebar/gate_runs`` index is
        a local handle only, with the same limited durability as ``run_workflow``.

        DISABLED unless REBAR_MCP_ALLOW_LLM=1 (it makes live, billable LLM calls)."""
        if not allow_llm():
            raise ValueError(
                "verify_completion_start is disabled: it makes a live, billable LLM call. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        ro = readonly()
        return _start_gate_job(
            "verify_completion",
            ticket_id,
            ref=ref,
            source=source,
            variant=f"graph={graph};source={source or 'attested'}",
            readonly=ro,
            force=False,
            work=lambda: _verify_completion_body(ticket_id, graph, ref, source, readonly=ro),
        )


def register_llm_tools(mcp, ctx) -> None:
    """Register the LLM/agent tools on ``mcp`` (see module docstring)."""
    _allow_llm = ctx.allow_llm
    _readonly = ctx.readonly

    _ANN = tool_annotation_presets()
    _register_gate_start_tools(mcp, _ANN, _allow_llm, _readonly)

    @mcp.tool(annotations=_ANN["READ_ONLY_OPEN_WORLD"])
    def review_code(
        base: str = "HEAD~1",
        head: str = "HEAD",
        reviewers: list[str] | None = None,
        ref: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Run a multi-reviewer LLM code review of a git range (base..head) ->
        an aggregated review_result dict (findings carry agreement + reviewers).

        ``source=attested`` (default) reads file context from a snapshot pinned at ``ref``
        (default: the reviewed ``head``), a single ref/snapshot (no base+head snapshot pair);
        ``source=local`` reads the checkout. The diff is computed from ``REBAR_ROOT``'s object
        DB. Results carry ``source``/``verified_at_sha``/``signable``.

        DISABLED unless REBAR_MCP_ALLOW_LLM=1 (live, billable LLM call(s); reaches
        network + filesystem + git). Needs the 'agents' extra + an API key. Returns
        a plain dict and advertises NO outputSchema by design (documented
        NO_SCHEMA_EXEMPT) — its CLI/library --output json is pinned to
        review_result."""
        if not _allow_llm():
            raise ValueError(
                "review_code is disabled: it makes live, billable LLM call(s). "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        import rebar.llm

        try:
            return rebar.llm.review_code(
                base=base, head=head, reviewers=reviewers, ref=ref, source=source
            )
        except rebar.llm.LLMError as exc:
            return _structured_llm_failure(exc)

    @mcp.tool(annotations=_ANN["READ_ONLY_OPEN_WORLD"])
    def scan_spec(
        spec_text: str,
        batch_size: int = 5,
        ref: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Batch-scan the store's open epics against a specification -> a
        review_result dict (gaps/conflicts/overlaps), epics evaluated in batches.

        ``ref``/``source`` select the verified code (``attested`` snapshot at ``ref`` default
        ``origin/main``, else ``local`` checkout); results carry ``source``/``verified_at_sha``.

        DISABLED unless REBAR_MCP_ALLOW_LLM=1 (live, billable LLM call(s)). Needs
        the 'agents' extra + an API key. Returns a plain dict and advertises NO
        outputSchema by design (documented NO_SCHEMA_EXEMPT)."""
        if not _allow_llm():
            raise ValueError(
                "scan_spec is disabled: it makes live, billable LLM call(s). "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        import rebar.llm

        return rebar.llm.scan_epics_for_spec(
            spec_text, batch_size=batch_size, ref=ref, source=source
        )

    @mcp.tool(annotations=_ANN["READ_ONLY_OPEN_WORLD"])
    def verify_completion(
        ticket_id: str,
        graph: bool | None = None,
        ref: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Verify a ticket's completion requirements are met -> a completion_verdict dict
        {verdict: "PASS"|"FAIL", findings[], summary?, target, reviewers, runner, model,
        trace_id, source, verified_at_sha, signable}. Checks every acceptance/success/close
        criterion + definition of done (for bugs, that the bug is resolved) against the
        implementation; on FAIL, each finding carries the failing criterion, an explanation,
        and a source-code citation. In READONLY mode this runs a pure read (no sign, no
        sidecar); a writable server records (see below).

        ``graph`` is a tri-state: unspecified (``None``) uses the ticket-type default
        (an epic verifies its whole subtree; other types verify only their own criteria),
        while an explicit ``True``/``False`` forces subtree/own-criteria verification —
        so ``graph=False`` on an epic verifies just the epic's own criteria.

        ``source=attested`` (default) verifies a snapshot pinned at ``ref`` (default
        ``origin/main``) — reproducible, branch-independent — and records ``verified_at_sha``;
        ``source=local`` verifies the in-place checkout (never signed). ``REBAR_ROOT`` only
        locates the object DB. (The CLI close gate verifies attested HEAD; this tool defaults
        to origin/main for distributed verification of merged code.)

        RECORDS ITS RESULT ON THE TICKET unless the server is read-only: an attested,
        certifiable PASS SIGNS a completion-verifier attestation (which a later same-``ref``
        close REUSES to skip a duplicate, billable verifier run), and both PASS and FAIL emit
        the COMPLETION_VERDICT sidecar. Recording is best-effort — its outcome rides on the
        result's ``record`` field (``{signed, cause, sidecar_written, error}``) and never
        changes the verdict. A ``local`` verdict is never signed.

        IN-FLIGHT DE-DUPLICATION (bug d80d): a second concurrent call for the same
        ticket + basis + variant while a verify is already running ATTACHES to that run and
        shares its verdict — it does NOT start a second billable LLM pass — so a
        client-side ``-32001`` timeout followed by a retry no longer double-charges. Disable
        with ``REBAR_MCP_DEDUP=0``. For a long verify, prefer ``verify_completion_start`` +
        poll (see its docstring) so the caller gets a durable job handle instead of a
        transport timeout.

        DISABLED unless REBAR_MCP_ALLOW_LLM=1: this makes a live, billable LLM call and reaches
        the network + filesystem. Needs the 'agents' extra + a model API key. Returns a plain
        dict and advertises NO outputSchema by design — the result is model-produced, so it is
        a documented NO_SCHEMA_EXEMPT and is not auto-driven in CI."""
        if not _allow_llm():
            raise ValueError(
                "verify_completion is disabled: it makes a live, billable LLM call. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        # The tool body stays SYNC (the certified-tool in-flight gauge + SIGTERM drain
        # require it — _mcp_health.instrument_certified_tools fails loud on an async
        # certified tool); the singleflight de-dups concurrent worker threads underneath.
        ro = _readonly()
        return run_gate_singleflight(
            "verify_completion",
            ticket_id,
            ref=ref,
            source=source,
            variant=f"graph={graph};source={source or 'attested'}",
            readonly=ro,
            force=False,
            work=lambda: _verify_completion_body(ticket_id, graph, ref, source, readonly=ro),
        )

    @mcp.tool(annotations=_ANN["READ_ONLY_OPEN_WORLD"])
    def review_plan(
        ticket_id: str,
        ref: str | None = None,
        source: str | None = None,
        force: bool = False,
    ) -> dict:
        """Run the plan-review gate on a ticket -> a plan_review_verdict dict
        {verdict: "PASS"|"BLOCK"|"INDETERMINATE", blocking[], advisory[], coaching[],
        indeterminate[], coverage, signature?, source, verified_at_sha, ...}. A deterministic
        Layer-1 floor (P1-P9) plus a four-pass (find -> verify -> decide -> coach) review of the
        ticket's whole plan — the inverse of verify_completion. On a non-blocking PASS it signs a
        plan-review attestation (so a subsequent claim passes the gate when enabled) and emits
        the REVIEW_RESULT sidecar; in READONLY mode it runs a pure read (no sign, no sidecar).

        When the ticket is UNCHANGED and already carries a still-valid plan-review
        attestation, the review SHORT-CIRCUITS (no LLM call) and reuses it; pass
        ``force=True`` to bypass that and force a full re-review.

        NOT-CLAIMABLE FAST-FAIL (no LLM): if the ticket cannot be claimed yet — status
        ``closed``/``idea``/``blocked``, or ``open`` but still blocked by an unclosed
        dependency — it returns an unsigned INDETERMINATE verdict (``coverage.llm_ran=false``,
        an ``indeterminate`` finding ``ticket-not-claimable``) instead of a billable review,
        since the review's only product is a claim attestation the ticket cannot use yet.
        ``in_progress`` tickets are never fast-failed and ``force=True`` bypasses this too.

        ``source=attested`` (default) reviews a snapshot pinned at ``ref`` (default
        ``origin/main``) and binds that SHA into the attestation so the claim gate re-hashes the
        SAME basis; ``source=local`` reviews the in-place checkout. ``REBAR_ROOT`` only locates
        the object DB.

        PASSED-BUT-UNSIGNED: a PASS whose attestation failed to persist is NOT a success — a
        subsequent ``claim`` still fails the gate, because the signature the gate consumes was
        never written. Every result therefore carries ``attestation``
        {signed, retryable, cause, error, recovery_tool, message}: branch on
        ``attestation.retryable`` (do NOT proceed to ``claim``) and call the tool named by
        ``attestation.recovery_tool`` — ``sign_review`` for a transient sign failure (cheap, no
        LLM), or ``review_plan`` again when ``cause`` is ``plan_changed`` /
        ``relation_unreadable`` / ``sidecar_lost`` (the last meaning nothing durable survived,
        so there is nothing to re-sign). ``cause`` is ``signed``/``skipped`` when nothing is
        wrong.

        IN-FLIGHT DE-DUPLICATION (bug d80d): a second concurrent call for the same
        ticket + basis while a review is already running ATTACHES to that run and shares its
        verdict — it does NOT start a second billable LLM review — so a client-side ``-32001``
        timeout followed by a retry no longer double-charges. ``force=True`` bypasses de-dup
        (a forced fresh review must not attach); disable entirely with ``REBAR_MCP_DEDUP=0``.
        For a long review, prefer ``review_plan_start`` + ``plan_review_status`` poll (see
        those docstrings) so the caller gets a durable job handle instead of a timeout.

        DISABLED unless REBAR_MCP_ALLOW_LLM=1: this makes live, billable LLM calls and reaches
        the network + filesystem. Needs the 'agents' extra + a model API key. Returns a plain
        dict and advertises NO outputSchema by design (model-produced result; NO_SCHEMA_EXEMPT)."""
        if not _allow_llm():
            raise ValueError(
                "review_plan is disabled: it makes live, billable LLM calls. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        # The tool body stays SYNC (see verify_completion): the singleflight de-dups
        # concurrent worker threads while the certified-tool gauge keeps counting billable
        # work. force=True bypasses de-dup, mirroring the gate's own force short-circuit bypass.
        ro = _readonly()
        return run_gate_singleflight(
            "plan_review",
            ticket_id,
            ref=ref,
            source=source,
            variant=f"source={source or 'attested'}",
            readonly=ro,
            force=force,
            work=lambda: _review_plan_body(ticket_id, ref, source, force, readonly=ro),
        )

    @mcp.tool(annotations=_ANN["MUTATE"])
    def sign_review(ticket_id: str) -> dict:
        """Cheaply (re)persist the plan-review attestation for an already-computed, still-valid
        PASS verdict from the latest REVIEW_RESULT sidecar -> {ok, signed, ticket_id, verdict,
        reason, signature?}. WITHOUT re-running the multi-pass LLM review (no LLM, no network).

        The recovery path (ticket middle-actinium-thrush) for a review_plan that computed a
        signable PASS but failed to persist the signature the claim gate consumes. REFUSES
        (ok=False) with a reason when there is no PASS sidecar, or the plan changed since the
        review (stale — run review_plan for a fresh verdict). NEVER signs a non-PASS / degraded /
        stale verdict.

        Unlike review_plan this is NOT gated on REBAR_MCP_ALLOW_LLM (it makes no LLM call), but it
        WRITES a SIGNATURE event, so it is disabled in REBAR_MCP_READONLY mode."""
        if _readonly():
            raise ValueError(
                "sign_review is disabled: it writes a SIGNATURE event (readonly mode)."
            )
        import rebar.llm

        return rebar.llm.resign_plan_review(ticket_id)
