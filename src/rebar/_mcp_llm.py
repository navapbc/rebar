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

from rebar._mcp_models import tool_annotation_presets
from rebar._operation_config import _shadow


def _structured_llm_failure(exc: Exception) -> dict:
    """Convert a raised ``LLMError`` into a STRUCTURED MCP tool RESULT (story
    authorial-hated-blackbear) rather than letting it propagate as an opaque FastMCP tool
    error. The driving agent can then branch on ``retryable`` (retry vs. escalate) instead of
    string-parsing an error. Carries the classifier disposition (``resolution_class`` /
    ``diagnostic``) when the raised error had one attached (mamba's run seam / preflight)."""
    from rebar.llm.failure import outcome_of

    o = outcome_of(exc)
    return {
        "error": "llm_unavailable",
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


def register_llm_tools(mcp, ctx) -> None:
    """Register the LLM/agent tools on ``mcp`` (see module docstring)."""
    _allow_llm = ctx.allow_llm
    _readonly = ctx.readonly

    _ANN = tool_annotation_presets()

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
        _shadow("mcp.llm.review_code")
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
        _shadow("mcp.llm.scan_spec")
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
        and a source-code citation. Read-only.

        ``graph`` is a tri-state: unspecified (``None``) uses the ticket-type default
        (an epic verifies its whole subtree; other types verify only their own criteria),
        while an explicit ``True``/``False`` forces subtree/own-criteria verification —
        so ``graph=False`` on an epic verifies just the epic's own criteria.

        ``source=attested`` (default) verifies a snapshot pinned at ``ref`` (default
        ``origin/main``) — reproducible, branch-independent — and records ``verified_at_sha``;
        ``source=local`` verifies the in-place checkout (never signed). ``REBAR_ROOT`` only
        locates the object DB. (The CLI close gate verifies attested HEAD; this tool defaults
        to origin/main for distributed verification of merged code.)

        DISABLED unless REBAR_MCP_ALLOW_LLM=1: this makes a live, billable LLM call and reaches
        the network + filesystem. Needs the 'agents' extra + a model API key. Returns a plain
        dict and advertises NO outputSchema by design — the result is model-produced, so it is
        a documented NO_SCHEMA_EXEMPT and is not auto-driven in CI."""
        _shadow("mcp.llm.verify_completion")
        if not _allow_llm():
            raise ValueError(
                "verify_completion is disabled: it makes a live, billable LLM call. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        import rebar.llm

        try:
            return rebar.llm.verify_completion(ticket_id, graph=graph, ref=ref, source=source)
        except rebar.llm.LLMError as exc:
            return _structured_llm_failure(exc)

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

        DISABLED unless REBAR_MCP_ALLOW_LLM=1: this makes live, billable LLM calls and reaches
        the network + filesystem. Needs the 'agents' extra + a model API key. Returns a plain
        dict and advertises NO outputSchema by design (model-produced result; NO_SCHEMA_EXEMPT)."""
        _shadow("mcp.llm.review_plan")
        if not _allow_llm():
            raise ValueError(
                "review_plan is disabled: it makes live, billable LLM calls. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        import rebar.llm
        from rebar.llm.plan_review.resign import classify_plan_review_attestation

        ro = _readonly()
        try:
            result = rebar.llm.review_plan(
                ticket_id, ref=ref, source=source, sign=not ro, emit_sidecar=not ro, force=force
            )
        except rebar.llm.LLMError as exc:
            return _structured_llm_failure(exc)
        # The CLI maps this same classification to exit 11; MCP has no exit code, so the
        # structured verdict rides on the payload instead (ticket ammonic-amoral-nabarlek).
        return _with_attestation(result, classify_plan_review_attestation)

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
        _shadow("mcp.llm.sign_review")
        if _readonly():
            raise ValueError(
                "sign_review is disabled: it writes a SIGNATURE event (readonly mode)."
            )
        import rebar.llm

        return rebar.llm.resign_plan_review(ticket_id)
