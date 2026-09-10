"""Register rebar's LLM-backed MCP tools.

Tools remain discoverable but reject live, billable calls unless
``REBAR_MCP_ALLOW_LLM`` is enabled. Read-only context suppresses review artifacts and
signatures. Model-produced results stay plain dictionaries without output-model imports.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any

from rebar._mcp_inflight import GateJobHandle, begin_gate_job, run_gate_singleflight
from rebar._mcp_models import tool_annotation_presets
from rebar._opcert_binding import spawn_context_daemon


def _structured_llm_failure(exc: Exception) -> dict:
    """Turn an ``LLMError`` into a machine-actionable MCP result.

    The shared taxonomy supplies the precise error code; an attached failure outcome
    contributes resolution class, retryability, and diagnostic instead of opaque prose.
    """
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
    """Attach the shared persisted-attestation classification to a plan result.

    MCP has no CLI-style exit 11, so a PASS without the claim-gate signature carries
    structured retryability and recovery-tool fields instead of appearing successful.
    """
    if isinstance(result, dict):
        result["attestation"] = classify(result).as_dict()
    return result


def _record_verify_completion(result, ticket_id: str, *, readonly: bool):
    """Record a completion verdict, or perform no store write in read-only mode.

    Writable runs emit the PASS/FAIL sidecar and sign reusable attested PASS results;
    the structured recording outcome is returned in ``record``.
    """
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
    """Run the synchronous plan unit shared by singleflight callers.

    It structures LLM failures and classifies signature persistence so one run produces
    one verdict, signature, and sidecar for every attached caller.
    """
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


def _verify_completion_body(
    ticket_id: str, graph: bool | None, ref, source, *, readonly: bool
) -> dict:
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


def _plan_review_sidecar_fields(gate_type: str, result: Any) -> dict[str, Any]:
    if gate_type != "plan_review" or not isinstance(result, dict):
        return {}
    out: dict[str, Any] = {}
    if "sidecar_emitted" in result:
        out["sidecar_emitted"] = bool(result.get("sidecar_emitted"))
    reviewed_at = result.get("sidecar_reviewed_at")
    if isinstance(reviewed_at, int) and not isinstance(reviewed_at, bool):
        out["sidecar_reviewed_at"] = reviewed_at
    return out


def _spawn_gate_daemon(
    handle: GateJobHandle, gate_type: str, ticket_id: str, work: Callable[[], Any]
) -> None:
    """Run gate work on a daemon and always settle its index and followers.

    ``spawn_context_daemon`` carries the bound signer context. The daemon cannot
    survive process exit; ``gate_status`` converts a stranded running index to failure,
    while any signed gate attestation remains authoritative.
    """
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
            record = {
                "job_id": handle.job_id,
                "ticket_id": ticket_id,
                "gate_type": gate_type,
                "status": status,
                "verdict": verdict,
                "error": str(error) if error is not None else None,
                "finished_at": time.time(),
            }
            record.update(_plan_review_sidecar_fields(gate_type, result))
            rebar.llm.record_gate_run(record)
            handle.complete(result=result, error=error)

    # Context-propagating spawn: a bare threading.Thread would drop the op-cert signer
    # binding and sign the bound principal under an unbound genesis key (bug ff4a).
    spawn_context_daemon(_bg, name=f"rebar-gate-{gate_type}")


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
    """Reserve a slot and let only its leader record and spawn the gate.

    Return ``{job_id, ticket_id, gate_type, status: "running"}`` immediately;
    duplicate starts attach to its ID without launching another billable run.
    """
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
    # Followers neither spawn nor rewrite the last-writer-wins index, which could replace
    # an already-terminal leader record with a fresh running value.
    if handle.is_new:
        rebar.llm.record_gate_run(
            {
                "job_id": handle.job_id,
                "ticket_id": ticket_id,
                "gate_type": gate_type,
                "status": "running",
                "started_at": time.time(),
            }
        )
        _spawn_gate_daemon(handle, gate_type, ticket_id, work)
    return {
        "job_id": handle.job_id,
        "ticket_id": ticket_id,
        "gate_type": gate_type,
        "status": "running",
    }


def _register_gate_start_tools(mcp, ann, allow_llm, readonly) -> None:
    """Register async gate starters outside the complexity-capped main registrar."""

    @mcp.tool(annotations=ann["READ_ONLY_OPEN_WORLD"])
    async def review_plan_start(
        ticket_id: str,
        ref: str | None = None,
        source: str | None = None,
        force: bool = False,
    ) -> dict:
        """Start plan review and immediately return
        ``{job_id, ticket_id, gate_type, status: "running"}``.

        Prefer this timeout-proof path, then poll ``plan_review_status`` for the durable
        attestation or ``gate_status`` for run state and sidecar readability. Duplicate
        ticket/basis starts share a job; ``force=True`` creates a fresh one. The local
        index and daemon do not survive process exit. Requires ``REBAR_MCP_ALLOW_LLM=1``.
        """
        if not allow_llm():
            raise ValueError(
                "review_plan_start is disabled: it makes live, billable LLM calls. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        ro = readonly()
        # Basis resolution and index writes block, so keep them off the event loop.
        import anyio.to_thread  # deferred: keep this module importable with core deps only

        return await anyio.to_thread.run_sync(
            functools.partial(
                _start_gate_job,
                "plan_review",
                ticket_id,
                ref=ref,
                source=source,
                variant=f"source={source or 'attested'}",
                readonly=ro,
                force=force,
                work=lambda: _review_plan_body(ticket_id, ref, source, force, readonly=ro),
            )
        )

    @mcp.tool(annotations=ann["READ_ONLY_OPEN_WORLD"])
    async def verify_completion_start(
        ticket_id: str,
        graph: bool | None = None,
        ref: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Start completion verification and immediately return
        ``{job_id, ticket_id, gate_type, status: "running"}``.

        Prefer this path, then poll ``verify_completion_status`` for the durable verdict
        or ``gate_status`` for run state. ``graph=None`` uses the type default; booleans
        force subtree or own-criteria verification and share the sync dedup key. Duplicate
        starts attach to one job. Local daemon/index durability ends with the process.
        Requires ``REBAR_MCP_ALLOW_LLM=1``.
        """
        if not allow_llm():
            raise ValueError(
                "verify_completion_start is disabled: it makes a live, billable LLM call. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        ro = readonly()
        # Offload the blocking basis-SHA git shell-out + index write off the event loop
        # (see ``review_plan_start``).
        import anyio.to_thread  # deferred: keep this module importable with core deps only

        return await anyio.to_thread.run_sync(
            functools.partial(
                _start_gate_job,
                "verify_completion",
                ticket_id,
                ref=ref,
                source=source,
                variant=f"graph={graph};source={source or 'attested'}",
                readonly=ro,
                force=False,
                work=lambda: _verify_completion_body(ticket_id, graph, ref, source, readonly=ro),
            )
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
        ref: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Run the gate-backed LLM code review of a git range (base..head) ->
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
            return rebar.llm.review_code(base=base, head=head, ref=ref, source=source)
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
        """Verify applicable criteria and return a cited ``{verdict: PASS|FAIL,
        findings, target, reviewers, runner, model, trace_id, source, verified_at_sha,
        signable}`` result.

        ``graph=None`` uses the type default (epics include descendants); booleans force
        subtree or own criteria. Attested source defaults to ``origin/main`` and can sign;
        local source cannot. Writable servers best-effort record every sidecar and sign a
        reusable attested PASS, reporting that outcome in ``record``; readonly servers do
        neither. Concurrent identical calls share one run; prefer
        ``verify_completion_start`` for a durable client handle. Requires the agents extra,
        model credentials, and ``REBAR_MCP_ALLOW_LLM=1``. The model result intentionally
        has no output schema.
        """
        if not _allow_llm():
            raise ValueError(
                "verify_completion is disabled: it makes a live, billable LLM call. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        # Stay synchronous so certified-op instrumentation can gauge worker-thread calls.
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
        """Review a whole plan through the P1-P11 find/verify/decide/coach passes.

        Returns ``{verdict: PASS|BLOCK|INDETERMINATE, blocking, advisory, coaching,
        indeterminate, coverage, signature?, source, verified_at_sha}``.

        A writable PASS signs the claim attestation and emits REVIEW_RESULT; readonly is
        pure. Unchanged valid attestations short-circuit unless ``force=True``. Tickets
        that cannot be claimed fast-fail unsigned and without an LLM, except in-progress
        or forced work. Attested source binds the reviewed SHA; local reads the checkout.
        A PASS is unusable until ``attestation.signed``: follow its structured retry and
        recovery tool instead of claiming. Concurrent identical reviews share one run;
        force bypasses dedup, and ``review_plan_start`` is preferred for long calls.
        Requires the agents extra, model credentials, and ``REBAR_MCP_ALLOW_LLM=1``; the
        model result intentionally has no output schema.
        """
        if not _allow_llm():
            raise ValueError(
                "review_plan is disabled: it makes live, billable LLM calls. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        # Stay synchronous for certified-op gauging; force bypasses review reuse and dedup.
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
        """Persist a current PASS sidecar without an LLM and return
        ``{ok, signed, ticket_id, verdict, reason, signature?}``.

        Missing, stale, degraded, and non-PASS sidecars are refused with a reason.
        LLM enablement is unnecessary, but readonly mode disables this signature write.
        """
        if _readonly():
            raise ValueError(
                "sign_review is disabled: it writes a SIGNATURE event (readonly mode)."
            )
        import rebar.llm

        return rebar.llm.resign_plan_review(ticket_id)
