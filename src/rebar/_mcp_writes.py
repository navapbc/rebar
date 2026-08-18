"""Write-tool registrar for the rebar MCP server.

``register_write_tools(mcp, ctx)`` registers the ``REBAR_MCP_READONLY``-gated write
tools (create/transition/claim/reopen/comment/edit/link/unlink/tag/untag/archive/
compact/set_file_impact/set_verify_commands/log_session/sign_manifest/run_workflow).
Split out of ``rebar.mcp_server.build_server`` as a pure structural refactor — names,
signatures, docstrings, outputSchemas, and gating are behaviour-identical.

The read-only gate is enforced at REGISTRATION time exactly as before: when the
server is read-only NO write tool is registered (so they are absent from
``list_tools()``), which this registrar reproduces by returning early. Shared handles
(``_dump``, ``_allow_llm``, ``logger``) are captured off ``ctx`` and rebound to their
original local names so the tool bodies are copied verbatim. Output models are
imported at module level (FastMCP resolves return annotations against this module's
globals).
"""

from __future__ import annotations

from typing import Any, cast

import rebar
from rebar._mcp_models import (
    AttachCommitsResultOut,
    ClaimResultOut,
    CreateResultOut,
    FileImpactItemOut,
    SignResultOut,
    VerifyCommandItemOut,
    WriteAckOut,
    tool_annotation_presets,
)


# Bug vapoury-attack-lamb. The tickets-branch push is best-effort: on failure it WARNS and
# leaves the commits local. An MCP client never sees that warning — it reads the tool
# result, and the server's stderr handler writes to the SERVER. Measured against a real
# declining origin, comment_ticket returned {"result": "ok"} with two ticket commits
# stranded. So every write result carries the store's delivery status, read from the
# durable marker rebar._store.push_state records.
#
# These are module-level (not closures over the registrar): they capture nothing, and
# nesting them inside register_write_tools pushed that already-large function past its
# complexity ceiling.
def _push_status() -> dict:
    """The store's push-delivery status, or ``unknown`` if it cannot be read.

    A plain file read (no git subprocess), deliberately taken AFTER the write. On
    ``sync.push=async`` the detached child may not have finished, so a fresh failure can
    land after this returns; the marker is DURABLE, so the next write reports it. The
    guarantee is "you will be told", not "you will be told in the same call".
    """
    try:
        return rebar.push_status()
    except Exception:  # noqa: BLE001 — a status read must never fail a completed write
        return {"state": "unknown"}


def _ack(result: str = "ok", *, description_warning: str | None = None) -> Any:
    """The shared write ack: the pre-existing ``{"result": …}`` plus delivery status.

    ``description_warning`` rides the same reasoning as ``push_status`` (ticket 594b):
    the library logs the save-time cap notice, but an MCP client reads only the tool
    result, so it is carried as a result field. ``None`` when there is nothing to say.
    """
    return WriteAckOut.model_validate(
        {
            "result": result,
            "push_status": _push_status(),
            "description_warning": description_warning,
        }
    )


def _with_push(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach delivery status to a tool that returns a plain (schema-less) dict."""
    return {**payload, "push_status": _push_status()}


def _register_attach_commits(mcp, ann) -> None:
    """Register the ``attach_commits`` repair tool.

    Its own registrar rather than another nested ``def`` inside ``register_write_tools``:
    every nested function raises that already-large function's cyclomatic complexity, which
    the shrink-only complexity baseline gate caps.
    """

    @mcp.tool(annotations=ann["MUTATE_IDEMPOTENT"])
    def attach_commits(ticket_id: str, commits: list[str]) -> AttachCommitsResultOut:
        """Retroactively link commits to a ticket by SHA (union-add, idempotent).

        The repair path when a commit landed without a usable `rebar-ticket:` trailer:
        attaching the SHAs records a COMMITS event so the close gate can still tie the
        ticket to its change. Every SHA must resolve to a commit in this repository —
        validation is ALL-OR-NOTHING, so if any SHA is bad, nothing is recorded."""
        _shadow("mcp.write.attach_commits")
        return AttachCommitsResultOut.model_validate(rebar.attach_commits(ticket_id, commits))


def _register_bridge_projects_writes(mcp, ann) -> None:
    """Register the ``bridge_projects_set``/``bridge_projects_remove`` write tools.

    Its own registrar rather than more nested ``def``s inside ``register_write_tools``:
    every nested function raises that already-large function's cyclomatic complexity, which
    the shrink-only complexity baseline gate caps.
    """

    @mcp.tool(annotations=ann["MUTATE_IDEMPOTENT"])
    def bridge_projects_set(key: str, repos: list[str]) -> WriteAckOut:
        """Set a bridge project key's repos (REPLACE semantics; idempotent)."""
        _shadow("mcp.write.bridge_projects_set")
        rebar.bridge_projects_set(key, repos)
        return _ack()

    @mcp.tool(annotations=ann["MUTATE"])
    def bridge_projects_remove(key: str) -> WriteAckOut:
        """Remove a bridge project key from the mapping (error if absent)."""
        _shadow("mcp.write.bridge_projects_remove")
        rebar.bridge_projects_remove(key)
        return _ack()


def _shadow(surface: str) -> None:
    """Emit ONE diagnostic shadow snapshot for an MCP operation (RP-04 S1).

    Guarded and side-effect-free apart from the DEBUG diagnostic; it does NOT gate
    or alter the tool. A malformed/insecure config is swallowed by
    ``emit_shadow_snapshot`` (logged redacted, never raised), so the shadow never
    alters the tool's behavior."""
    from rebar._operation_config import emit_shadow_snapshot

    emit_shadow_snapshot(surface=surface)


def register_write_tools(mcp, ctx) -> None:
    """Register the write tools on ``mcp`` — a no-op on a read-only server.

    The registration-time read-only gate is IDENTICAL to the original in-line
    ``if not _readonly():`` guard: when read-only, register nothing (return early) so
    the write tools never appear in ``list_tools()``."""
    if ctx.readonly():
        return
    _dump = ctx.dump
    _allow_llm = ctx.allow_llm
    logger = ctx.logger

    _ANN = tool_annotation_presets()

    @mcp.tool(annotations=_ANN["MUTATE"])
    def create_ticket(
        ticket_type: str,
        title: str,
        parent: str | None = None,
        priority: int | None = None,
        assignee: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        bridge_project: str | None = None,
        repos: list[str] | None = None,
    ) -> CreateResultOut:
        """Create a ticket; returns {id, alias} (agents get the alias without
        a second show()). A non-null description_warning means the description exceeds
        the plan-review admission cap while the claim gate is on — the ticket was still
        created, but claiming it needs a review that refuses the description as-is."""
        _shadow("mcp.write.create_ticket")
        created = rebar.create_ticket(
            ticket_type,
            title,
            parent=parent,
            priority=priority,
            assignee=assignee,
            description=description,
            tags=tags,
            bridge_project=bridge_project,
            repos=repos,
            return_alias=True,
            _creation_channel="mcp",
        )
        return CreateResultOut.model_validate(_with_push(cast("dict[str, Any]", created)))

    @mcp.tool(annotations=_ANN["MUTATE"])
    def create_identity(
        name: str,
        email: str,
        mappings: list[dict] | None = None,
        keys: list[str] | None = None,
    ) -> CreateResultOut:
        """Create an identity entity: a gate-/graph-exempt ticket recording a
        person/agent. ``name`` is the title; ``email`` plus ``mappings`` (list of
        {provider, external_id}) and ``keys`` (OpenSSH authorized-keys lines) ride the
        CREATE and surface in show_ticket. Returns {id, alias}."""
        _shadow("mcp.write.create_identity")
        created = rebar.create_identity(
            name,
            email,
            mappings=mappings,
            keys=keys,
            return_alias=True,
            _creation_channel="mcp",
        )
        return CreateResultOut.model_validate(_with_push(cast("dict[str, Any]", created)))

    @mcp.tool(annotations=_ANN["MUTATE"])
    def create_idea(title: str, description: str | None = None) -> CreateResultOut:
        """Capture an undesigned idea: create an epic in status 'idea' atomically.

        The idea is born in status 'idea' via a single CREATE event (never momentarily
        'open'/claimable), is excluded from ready/next-batch, and 'idea -> closed'
        (reject) skips the completion gates. Promote a kept idea with
        transition_ticket(id, "idea", "open"). Returns {id, alias}."""
        _shadow("mcp.write.create_idea")
        return CreateResultOut.model_validate(
            _with_push(
                cast(
                    "dict[str, Any]",
                    rebar.idea(
                        title, description=description, return_alias=True, _creation_channel="mcp"
                    ),
                )
            )
        )

    @mcp.tool(annotations=_ANN["MUTATE"])
    def transition_ticket(
        ticket_id: str,
        current_status: str,
        target_status: str,
        close_class: str = "",
        force: bool = False,
        reason: str = "",
        caused_by: str = "",
        ref: str | None = None,
    ) -> dict:
        """Transition a ticket's status (optimistic concurrency). Returns the
        engine result {ticket_id, from, to, newly_unblocked}.

        ``open -> in_progress`` starts work and is gated by the plan-review gate
        (``verify.require_plan_review_for_claim``) exactly like ``claim_ticket``.
        ``force=true`` bypasses the applicable gate and ``reason`` records the
        operator's audit note. When force replaces otherwise-required certification,
        the absent certified attestation is the durable audit signal.

        ``close_class`` is REQUIRED to close a ``bug`` ticket (any ``*->closed``
        from a non-``idea`` status): pass one of the bounded classification values
        (``regression``, ``plan_defect``, ``env_integration``, ``flaky``,
        ``preexisting``, ``not_a_bug``, ``duplicate``, ``escalated``,
        ``undetermined``). It is IGNORED for non-bug closes and for non-closing
        transitions. Force does not relax this classification invariant: a bug
        close without a valid ``close_class`` is refused.

        ``caused_by`` records the explicit culprit for a bug close. ``ref`` selects
        the committed tree used by completion verification; it defaults to HEAD."""
        _shadow("mcp.write.transition_ticket")
        return _with_push(
            cast(
                "dict[str, Any]",
                rebar.transition(
                    ticket_id,
                    current_status,
                    target_status,
                    close_class=close_class,
                    force=force,
                    reason=reason,
                    caused_by=caused_by,
                    ref=ref,
                ),
            )
        )

    @mcp.tool(annotations=_ANN["MUTATE"])
    def claim_ticket(
        ticket_id: str,
        assignee: str | None = None,
        force: str | None = None,
    ) -> ClaimResultOut:
        """Atomically claim an OPEN ticket (-> in_progress + assignee).

        Raises a tool error (ConcurrencyError) if the ticket is not open —
        i.e. another agent already claimed it. ``force`` supplies the reason for
        bypassing the plan-review gate; when it replaces certification, the absent
        certified attestation is the durable audit signal.
        """
        _shadow("mcp.write.claim_ticket")
        return ClaimResultOut.model_validate(
            _with_push(
                cast(
                    "dict[str, Any]",
                    rebar.claim(ticket_id, assignee=assignee, force=force),
                )
            )
        )

    @mcp.tool(annotations=_ANN["MUTATE"])
    def reopen_ticket(ticket_id: str) -> dict:
        """Reopen a closed ticket (closed -> open). Optimistic-concurrency:
        raises a tool error if the ticket is not currently closed."""
        _shadow("mcp.write.reopen_ticket")
        return _with_push(cast("dict[str, Any]", rebar.reopen(ticket_id)))

    @mcp.tool(annotations=_ANN["MUTATE"])
    def comment_ticket(ticket_id: str, body: str) -> WriteAckOut:
        """Append a comment to a ticket."""
        _shadow("mcp.write.comment_ticket")
        rebar.comment(ticket_id, body)
        return _ack()

    @mcp.tool(annotations=_ANN["MUTATE"])
    def log_session(
        entry: str,
        summary: str | None = None,
        relates_to: str | None = None,
        discovered_from: str | None = None,
    ) -> CreateResultOut:
        """Append a verbose entry to the current session_log, creating one on
        first use (write-gated: refused under REBAR_MCP_READONLY=1). Returns the
        log's {id, alias}; optional relates_to / discovered_from link it to the
        work it documents."""
        _shadow("mcp.write.log_session")
        res = rebar.append_session_log(
            entry,
            summary=summary,
            relates_to=relates_to,
            discovered_from=discovered_from,
            _creation_channel="mcp",
        )
        return CreateResultOut.model_validate(
            _with_push({"id": res["id"], "alias": res.get("alias")})
        )

    @mcp.tool(annotations=_ANN["MUTATE"])
    def edit_ticket(
        ticket_id: str,
        title: str | None = None,
        priority: int | None = None,
        assignee: str | None = None,
        description: str | None = None,
        ticket_type: str | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
        set_tags: list[str] | None = None,
        bridge_project: str | None = None,
        repos: list[str] | None = None,
    ) -> WriteAckOut:
        """Edit ticket fields (title/priority/assignee/description/ticket_type).

        Tags mutate via convergent deltas: add_tags / remove_tags add/remove,
        or set_tags replaces the whole set (compiled to a delta; add-wins, so a
        concurrent remote add is never silently clobbered). set_tags is mutually
        exclusive with add_tags/remove_tags.

        bridge_project is promote-only — it may be set on an unbound ticket but is
        rejected once the ticket already holds a binding ("" marks never-sync); repos
        replaces the associated repositories and is freely editable. Both are
        present-only: leave them None to edit neither. Unlike the library edit_ticket
        (a **fields passthrough), this tool's signature is enumerated, so these two are
        forwarded explicitly to keep the MCP edit surface at parity with CLI/library.

        description_warning is non-null when the new description exceeds the plan-review
        admission cap while the claim gate is on: the edit still succeeded, but claiming
        the ticket will need a review that refuses it until the description is shorter.
        """
        _shadow("mcp.write.edit_ticket")
        description_warning = rebar.edit_ticket(
            ticket_id,
            title=title,
            priority=priority,
            assignee=assignee,
            description=description,
            ticket_type=ticket_type,
            add_tags=add_tags,
            remove_tags=remove_tags,
            set_tags=set_tags,
            bridge_project=bridge_project,
            repos=repos,
        )
        return _ack(description_warning=description_warning)

    @mcp.tool(annotations=_ANN["MUTATE"])
    def link_tickets(id1: str, id2: str, relation: str) -> WriteAckOut:
        """Link two tickets (one of the seven canonical relations: blocks |
        depends_on | relates_to | duplicates | supersedes | discovered_from |
        caused_by).

        Blocking relations are escalated to comparable endpoints when the two tickets
        do not share a parent, so the RECORDED edge may differ from the requested one.
        When that happens the return value names both pairs — otherwise the caller
        would be told "ok" for an edge that was never written.
        """
        _shadow("mcp.write.link_tickets")
        record = rebar.link(id1, id2, relation)
        if not record:
            return _ack()
        original = record.get("original") or {}
        resolved = record.get("resolved") or {}
        return _ack(
            f"ok (escalated: {original.get('source')}->{original.get('target')} "
            f"recorded as {resolved.get('source')}->{resolved.get('target')})"
        )

    @mcp.tool(annotations=_ANN["MUTATE"])
    def unlink_tickets(id1: str, id2: str) -> WriteAckOut:
        """Remove a link between two tickets."""
        _shadow("mcp.write.unlink_tickets")
        rebar.unlink(id1, id2)
        return _ack()

    @mcp.tool(annotations=_ANN["MUTATE_IDEMPOTENT"])
    def tag_ticket(ticket_id: str, tag: str) -> WriteAckOut:
        """Add a tag to a ticket."""
        _shadow("mcp.write.tag_ticket")
        rebar.tag(ticket_id, tag)
        return _ack()

    @mcp.tool(annotations=_ANN["MUTATE_IDEMPOTENT"])
    def untag_ticket(ticket_id: str, tag: str) -> WriteAckOut:
        """Remove a tag from a ticket."""
        _shadow("mcp.write.untag_ticket")
        rebar.untag(ticket_id, tag)
        return _ack()

    @mcp.tool(annotations=_ANN["DESTRUCTIVE"])
    def archive_ticket(ticket_id: str) -> WriteAckOut:
        """Archive a ticket (excludes it from the default list)."""
        _shadow("mcp.write.archive_ticket")
        rebar.archive(ticket_id)
        return _ack()

    @mcp.tool(annotations=_ANN["DESTRUCTIVE"])
    def compact_ticket(ticket_id: str | None = None) -> WriteAckOut:
        """Compact a ticket's event log (or all tickets if id omitted)."""
        _shadow("mcp.write.compact_ticket")
        rebar.compact(ticket_id)
        return _ack()

    # ── File-impact / verify-commands writes (WS5d; feed next-batch) ───────
    # Typed item params so the tools advertise an inputSchema (the {path,reason}
    # / {dd_id,dd_text,command} shapes mirror the get_* output models + schemas).
    @mcp.tool(annotations=_ANN["MUTATE_IDEMPOTENT"])
    def set_file_impact(ticket_id: str, impact: list[FileImpactItemOut]) -> WriteAckOut:
        """Record file impact (list of {path, reason}) for conflict-aware
        next-batch scheduling."""
        _shadow("mcp.write.set_file_impact")
        rebar.set_file_impact(ticket_id, [_dump(e) for e in impact])
        return _ack()

    @mcp.tool(annotations=_ANN["MUTATE_IDEMPOTENT"], structured_output=False)
    def declare_no_file_impact(ticket_id: str, reason: str) -> str:
        """Declare that a ticket has no repository-file impact, with a reason."""
        _shadow("mcp.write.declare_no_file_impact")
        rebar.declare_no_file_impact(ticket_id, reason)
        return "ok"

    @mcp.tool(annotations=_ANN["MUTATE_IDEMPOTENT"])
    def set_verify_commands(ticket_id: str, commands: list[VerifyCommandItemOut]) -> WriteAckOut:
        """Record DD-level verify commands (list of {dd_id, dd_text, command})."""
        _shadow("mcp.write.set_verify_commands")
        rebar.set_verify_commands(ticket_id, [_dump(e) for e in commands])
        return _ack()

    _register_attach_commits(mcp, _ANN)
    _register_bridge_projects_writes(mcp, _ANN)

    @mcp.tool(annotations=_ANN["MUTATE"])
    def sign_manifest(ticket_id: str, manifest: list[str]) -> SignResultOut:
        """Sign a manifest of verified steps as an asymmetric op-cert.

        Mints a rebar.opcert.v1 DSSE op-cert over the steps with this
        environment's Ed25519 key (the gitignored .opcert-key) and records a
        SIGNATURE event. Returns {ticket_id, manifest, algorithm:'sshsig',
        envelope, principal, material_fingerprint, merged_log_commit,
        head_sha, signed_at}. The op-cert kinds (plan-review /
        completion-verifier) are signed and accepted ONLY as op-certs — the
        legacy symmetric HMAC scheme is retired for them (story 8f1d). Use
        verify_signature to certify it later."""
        _shadow("mcp.write.sign_manifest")
        return SignResultOut.model_validate(
            _with_push(cast("dict[str, Any]", rebar.sign_manifest(ticket_id, manifest)))
        )

    @mcp.tool(annotations=_ANN["MUTATE_OPEN_WORLD"])
    async def run_workflow(
        workflow: str,
        ticket_id: str,
        inputs: dict | None = None,
        dry_run: bool = False,
        ref: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Start a workflow run; returns {run_id, ticket_id, status:'running'}
        IMMEDIATELY (async — the run executes on a background **daemon thread**, so
        it survives client request timeouts). Poll get_workflow_status /
        get_workflow_result to read its outcome. DURABILITY IS LIMITED: the daemon
        thread does NOT survive the MCP process exiting, and there is NO reaper or
        automatic resume — if the process dies mid-run the run is left ``running``
        forever and nothing re-drives it. Step effects ARE persisted to
        ``ticket_id``'s event log with idempotency markers, so a run can be
        **resumed only by explicitly re-invoking it** (already-completed steps are
        then skipped); it does not resume on its own. ``workflow`` is a
        .rebar/workflows/<name> name or a file path; ``dry_run`` executes agent
        steps with the offline FakeRunner (no tokens). Write tool (gated by
        REBAR_MCP_READONLY).

        A workflow with LLM/agent steps reads a snapshot pinned at ``ref`` (default
        ``origin/main``) in ``source=attested`` (default) mode — never the server's
        mutable checkout — and is DISABLED unless REBAR_MCP_ALLOW_LLM=1 (it makes
        live, billable LLM calls), exactly like the other agentic tools. A
        deterministic-only workflow needs neither."""
        _shadow("mcp.write.run_workflow")
        import threading

        from rebar.llm.workflow import executor as _wf_exec
        from rebar.llm.workflow import runs as _wf_runs

        # A workflow that runs tool-using agents is a live, billable LLM op — fence it
        # behind the SAME gate as review_*/verify_* (dry_run is offline, so exempt).
        if not dry_run:
            try:
                _doc = _wf_runs.load_workflow_doc(workflow, None)
            except Exception:  # noqa: BLE001 — a load error surfaces in the run record below
                _doc = None
            if _doc is not None and _wf_runs.has_llm_steps(_doc) and not _allow_llm():
                raise ValueError(
                    f"run_workflow on {workflow!r} is disabled: it runs tool-using LLM "
                    "agent steps (a live, billable LLM call). Set REBAR_MCP_ALLOW_LLM=1 "
                    "to enable it, or pass dry_run=true for the offline runner."
                )

        run_id = _wf_exec.new_run_id()
        # Record the index AND an initial 'running' marker BEFORE returning, so an
        # immediate get_workflow_status poll resolves and sees the run (the
        # background thread overwrites the record with the full result, LWW).
        _wf_runs.record_run_location(run_id, ticket_id, None)
        _wf_exec.TicketEventRecorder(ticket_id).run_started(
            {"run_id": run_id, "status": "running", "workflow_name": workflow}
        )

        def _bg() -> None:
            # Step failures already persist a failed step record via the executor.
            # A failure BEFORE the executor loop (workflow-not-found, validation
            # error) would otherwise leave the run stuck at 'running' forever, so
            # flip the run record to 'failed' here — a poller then settles instead
            # of spinning to its timeout.
            try:
                _wf_runs.run(
                    workflow,
                    inputs or {},
                    ticket_id=ticket_id,
                    run_id=run_id,
                    dry_run=dry_run,
                    ref=ref,
                    source_mode=source,
                )
            except Exception as exc:  # noqa: BLE001 — background run failure is reflected in run-state, not raised
                try:
                    _wf_exec.TicketEventRecorder(ticket_id).run_finished(
                        {"run_id": run_id, "status": "failed", "error": str(exc)}
                    )
                except Exception:
                    # Don't let a failure in the error-reporting path hide the
                    # original run failure: log BOTH (the recorder error with its
                    # traceback, and the original run error it was trying to record).
                    logger.warning(
                        "failed to record workflow run %s failure (original run error: %s)",
                        run_id,
                        exc,
                        exc_info=True,
                    )

        threading.Thread(target=_bg, daemon=True).start()
        return {"run_id": run_id, "ticket_id": ticket_id, "status": "running"}
