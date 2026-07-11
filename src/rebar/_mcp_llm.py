"""LLM-tool registrar for the rebar MCP server.

``register_llm_tools(mcp, ctx)`` registers the ``REBAR_MCP_ALLOW_LLM``-gated agent
tools (review_ticket, review_code, scan_spec, verify_completion, review_plan). Split
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


def _structured_llm_failure(exc: Exception) -> dict:
    """Convert a raised ``LLMError`` into a STRUCTURED MCP tool RESULT (story
    authorial-hated-blackbear) rather than letting it propagate as an opaque FastMCP tool
    error. The driving agent can then branch on ``retryable`` (retry vs. escalate) instead of
    string-parsing an error. Carries the classifier disposition (``resolution_class`` /
    ``diagnostic``) when the raised error had one attached (mamba's run seam / preflight)."""
    from rebar.llm.failure import outcome_of

    o = outcome_of(exc)
    return {
        "error": str(exc),
        "resolution_class": o.resolution_class.value if o is not None else None,
        "retryable": bool(o.retryable) if o is not None else False,
        "diagnostic": o.diagnostic if o is not None else None,
    }


def register_llm_tools(mcp, ctx) -> None:
    """Register the LLM/agent tools on ``mcp`` (see module docstring)."""
    _allow_llm = ctx.allow_llm
    _readonly = ctx.readonly

    _ANN = tool_annotation_presets()

    @mcp.tool(annotations=_ANN["READ_ONLY_OPEN_WORLD"])
    def review_ticket(
        ticket_id: str,
        reviewer_id: str | None = None,
        graph: bool = False,
        ref: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Run an LLM review of a ticket (or its graph) -> a review_result dict
        {findings[], target, reviewers, runner, model, trace_id, summary, source,
        verified_at_sha, signable}.

        ``ref``/``source`` select the verified code: ``source=attested`` (default) reads a
        snapshot pinned at ``ref`` (default ``origin/main``) — NEVER the server's checked-out
        branch — and records ``verified_at_sha``; ``source=local`` reads the in-place checkout
        (unsigned). ``REBAR_ROOT`` only locates the object DB to fetch from.

        DISABLED unless REBAR_MCP_ALLOW_LLM=1: this makes a live, billable LLM call
        and reaches the network + filesystem (it is not a plain store read). It
        needs the 'agents' extra + a model API key (provider per REBAR_LLM_MODEL,
        e.g. ANTHROPIC_API_KEY or OPENAI_API_KEY). Returns a plain dict and
        advertises NO outputSchema by design — the result is model-produced, so it
        is a documented NO_SCHEMA_EXEMPT and is not auto-driven in CI."""
        if not _allow_llm():
            raise ValueError(
                "review_ticket is disabled: it makes a live, billable LLM call. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        import rebar.llm

        return rebar.llm.review_ticket(ticket_id, reviewer_id, graph=graph, ref=ref, source=source)

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

        ``source=attested`` (default) reviews a snapshot pinned at ``ref`` (default
        ``origin/main``) and binds that SHA into the attestation so the claim gate re-hashes the
        SAME basis; ``source=local`` reviews the in-place checkout. ``REBAR_ROOT`` only locates
        the object DB.

        DISABLED unless REBAR_MCP_ALLOW_LLM=1: this makes live, billable LLM calls and reaches
        the network + filesystem. Needs the 'agents' extra + a model API key. Returns a plain
        dict and advertises NO outputSchema by design (model-produced result; NO_SCHEMA_EXEMPT)."""
        if not _allow_llm():
            raise ValueError(
                "review_plan is disabled: it makes live, billable LLM calls. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        import rebar.llm

        ro = _readonly()
        try:
            return rebar.llm.review_plan(
                ticket_id, ref=ref, source=source, sign=not ro, emit_sidecar=not ro, force=force
            )
        except rebar.llm.LLMError as exc:
            return _structured_llm_failure(exc)

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
