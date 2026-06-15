"""rebar MCP server (FastMCP).

Exposes the ticket system as MCP tools, built on the rebar Python library.
Reads (``show``/``list``) run in-process via rebar._reads (no subprocess);
``reconcile`` defaults to a non-mutating dry-run.

Safety:
  * ``reconcile`` defaults to ``dry-run``; ``live`` additionally requires
    REBAR_MCP_ALLOW_RECONCILE_LIVE=1.
  * Write tools (create/transition/edit/link/unlink/tag/untag/archive/comment)
    are gated by REBAR_MCP_READONLY: set it to 1 to expose a read-only server.

The ``mcp`` dependency is an optional extra and is imported lazily.
"""

from __future__ import annotations

import importlib.util
import os

import rebar

# The reconcile tool gates modes by the engine's canonical MODE_CAPS table, which
# lives in the bundled engine at rebar_reconciler/mode.py. We load it ONCE here by
# FILE PATH (not `from rebar_reconciler.mode import ...`) and bind the names as
# module globals. Loading by path is deliberate: the dotted import is unreliable
# because the top-level name `rebar_reconciler` is shadowed in sys.modules in some
# contexts (notably the unit-test package of the same name under pytest), which
# makes `rebar_reconciler.mode` raise ModuleNotFoundError. mode.py is stdlib-only
# and self-contained, so a standalone path-load is safe.
def _load_engine_mode():
    from rebar._engine import engine_dir

    mode_path = engine_dir() / "rebar_reconciler" / "mode.py"
    spec = importlib.util.spec_from_file_location("rebar._engine_mode", mode_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MODE_CAPS, mod.Mode


MODE_CAPS, Mode = _load_engine_mode()

# Typed output for show_ticket so FastMCP advertises an outputSchema to MCP
# clients (agents get a documented, validated shape instead of an opaque dict).
# Mirrors src/rebar/schemas/ticket_state.schema.json — kept permissive
# (extra="allow", non-core fields optional) so the evolving event-sourced shape
# never breaks the tool. A cross-interface test pins this and the CLI/library
# output to the canonical schema.
#
# Defined at module level (not inside build_server) because FastMCP resolves
# return annotations via eval against the function's module globals; a local
# class can't be resolved under `from __future__ import annotations`. pydantic is
# guaranteed by the `mcp` extra; guarded so a bare `import rebar.mcp_server`
# without the extra still reaches build_server's friendly install message.
try:
    from pydantic import BaseModel, ConfigDict

    class _Out(BaseModel):
        # Permissive base: extra fields allowed so the evolving event-sourced
        # shapes never break a tool. Each model mirrors a src/rebar/schemas file;
        # the cross-interface schema tests pin both to the canonical schema.
        model_config = ConfigDict(extra="allow")

    class TicketStateOut(_Out):
        ticket_id: str
        ticket_type: str
        title: str
        status: str
        priority: int
        tags: list[str] = []
        assignee: str | None = None
        parent_id: str | None = None
        alias: str | None = None
        description: str | None = None
        comments: list[dict] = []
        deps: list[dict] = []
        file_impact: list[dict] = []

    class DepsGraphOut(_Out):
        ticket_id: str
        deps: list[dict] = []
        blockers: list[str] = []
        children: list[str] = []
        ready_to_work: bool

    class NextBatchOut(_Out):
        epic_id: str

    class ClarityResultOut(_Out):
        score: int
        verdict: str
        threshold: int
        passed: bool | None = None

    class ValidateReportOut(_Out):
        score: int
        critical_issues: list = []
        major_issues: list = []
        minor_issues: list = []
        warnings: list = []
        suggestions: list = []

    class FileImpactItemOut(_Out):
        path: str
        reason: str | None = None

    class VerifyCommandItemOut(_Out):
        command: str
        dd_id: str | None = None
        dd_text: str | None = None

    class CreateResultOut(_Out):
        id: str
        alias: str | None = None

    class ClaimResultOut(_Out):
        ticket_id: str
        status: str
        assignee: str | None = None

    class GateResultOut(_Out):
        verdict: str
        reason: str
        passed: bool | None = None

    class ListEpicsOut(_Out):
        # Mirrors src/rebar/schemas/list_epics.schema.json ({p0_bugs, epics}).
        p0_bugs: list[dict] = []
        epics: list[dict] = []

    class BridgeFsckOut(_Out):
        # Mirrors src/rebar/schemas/bridge_fsck.schema.json.
        orphaned: list = []
        duplicates: list = []
        stale: list = []

    # NOTE: transition/reopen return {ticket_id, from, to, newly_unblocked}; the
    # `from` key is a Python reserved word, so those tools return a plain dict
    # (FastMCP serializes it correctly) rather than a typed model. They therefore
    # advertise no outputSchema by design — a documented exemption pinned in
    # tests/interfaces/test_mcp_output_schema_coverage.py. Their CLI/library JSON
    # is still pinned to transition_result by test_schema_outputs.py.
except ImportError:  # pragma: no cover - pydantic ships with the mcp extra
    TicketStateOut = None  # type: ignore[assignment,misc]
    DepsGraphOut = ClarityResultOut = ValidateReportOut = None  # type: ignore[assignment,misc]
    NextBatchOut = FileImpactItemOut = VerifyCommandItemOut = None  # type: ignore[assignment,misc]
    CreateResultOut = ClaimResultOut = GateResultOut = None  # type: ignore[assignment,misc]
    ListEpicsOut = BridgeFsckOut = None  # type: ignore[assignment,misc]


def _env_truthy(name: str) -> bool:
    """Case-insensitive truthy parse for a boolean env gate.

    Accepts 1 / true / yes (any case, surrounding whitespace tolerated). Used by
    BOTH REBAR_MCP_READONLY and REBAR_MCP_ALLOW_RECONCILE_LIVE so a common
    spelling like ``TRUE`` can never silently fail open on the readonly gate
    (bug ship-mogul-glob).
    """
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _readonly() -> bool:
    return _env_truthy("REBAR_MCP_READONLY")


def _dump(item):
    """Normalize a typed list-item param to a plain dict (FastMCP may deliver a
    validated pydantic model or a raw dict depending on version). Drops keys whose
    value is None so the engine receives a clean {path,reason}/{dd_id,…} object."""
    if hasattr(item, "model_dump"):
        return {k: v for k, v in item.model_dump().items() if v is not None}
    return item


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "The rebar MCP server requires the 'mcp' extra. "
            "Install it with: pip install 'rebar[mcp]'"
        ) from exc

    mcp = FastMCP("rebar")

    # ── Read tools ────────────────────────────────────────────────────────────
    @mcp.tool()
    def show_ticket(ticket_id: str) -> TicketStateOut:
        """Show compiled ticket state (accepts full id, short id, or alias)."""
        return TicketStateOut.model_validate(rebar.show_ticket(ticket_id))

    @mcp.tool()
    def list_tickets(
        status: str | None = None,
        ticket_type: str | None = None,
        priority: int | None = None,
        parent: str | None = None,
        has_tag: str | None = None,
        without_tag: str | None = None,
        include_archived: bool = False,
        exclude_deleted: bool = False,
        min_children: int | None = None,
        blocking_state: str = "",
        with_children_count: bool = False,
    ) -> list[TicketStateOut]:
        """List tickets as a JSON array, with optional filters.

        ``exclude_deleted`` drops tickets whose reduced status is ``deleted``.
        delete writes STATUS(deleted)+ARCHIVED, so the default list already hides
        tombstones via archived-exclusion; ``exclude_deleted`` only changes
        results when combined with ``include_archived=True``. Each item carries a
        ``children_count``; ``min_children`` keeps tickets with >= N direct
        children, and ``blocking_state`` ("unblocked"/"blocked") filters by
        readiness (all blockers closed vs an open blocker).
        """
        return [
            TicketStateOut.model_validate(t)
            for t in rebar.list_tickets(
                status=status,
                ticket_type=ticket_type,
                priority=priority,
                parent=parent,
                has_tag=has_tag,
                without_tag=without_tag,
                include_archived=include_archived,
                exclude_deleted=exclude_deleted,
                min_children=min_children,
                blocking_state=blocking_state,
                with_children_count=with_children_count,
            )
        ]

    @mcp.tool()
    def ticket_deps(ticket_id: str) -> DepsGraphOut:
        """Show the dependency graph for a ticket."""
        return DepsGraphOut.model_validate(rebar.deps(ticket_id))

    @mcp.tool()
    def ready_tickets() -> list[TicketStateOut]:
        """List tickets ready to work (all blockers closed)."""
        return [TicketStateOut.model_validate(t) for t in rebar.ready()]

    @mcp.tool()
    def next_batch(epic_id: str) -> NextBatchOut:
        """Next parallel batch of unblocked tickets under an epic's hierarchy."""
        return NextBatchOut.model_validate(rebar.next_batch(epic_id))

    @mcp.tool()
    def search(
        query: str,
        status: str | None = None,
        ticket_type: str | None = None,
        has_tag: str | None = None,
        include_archived: bool = False,
    ) -> list[TicketStateOut]:
        """Full-text search over titles/descriptions/comments/tags (replay-derived)."""
        return [
            TicketStateOut.model_validate(t)
            for t in rebar.search(
                query,
                status=status,
                ticket_type=ticket_type,
                has_tag=has_tag,
                include_archived=include_archived,
            )
        ]

    @mcp.tool()
    def fsck(recover: bool = False) -> str:
        """Check ticket-store integrity (JSON validity, CREATE presence, lock
        cleanup). Set recover=True to run the recovery path."""
        if recover and _readonly():
            raise ValueError(
                "fsck recover=True is a write operation and is disabled: this "
                "server is read-only (REBAR_MCP_READONLY)"
            )
        # Plain fsck still mutates: it removes a stale .git/index.lock. On a
        # read-only server suppress that write (report the stale lock instead).
        return rebar.fsck(recover=recover, report_only=_readonly())

    # ── Quality gates + file-impact reads (WS5d) ───────────────────────────────
    @mcp.tool()
    def clarity_check(ticket_id: str) -> ClarityResultOut:
        """Score ticket clarity (score / verdict / threshold / passed)."""
        return ClarityResultOut.model_validate(rebar.clarity_check(ticket_id))

    @mcp.tool()
    def check_ac(ticket_id: str) -> GateResultOut:
        """Check the ticket has an Acceptance Criteria block
        ({verdict, criteria_count, reason, passed})."""
        return GateResultOut.model_validate(rebar.check_ac(ticket_id))

    @mcp.tool()
    def quality_check(ticket_id: str) -> GateResultOut:
        """Check ticket dispatch readiness ({verdict, line_count, keyword_count,
        ac_items, file_impact, reason, passed})."""
        return GateResultOut.model_validate(rebar.quality_check(ticket_id))

    @mcp.tool()
    def validate() -> ValidateReportOut:
        """Repo-wide quality health check (JSON report: score, critical/major/
        minor issues, warnings, suggestions). Takes no ticket id."""
        return ValidateReportOut.model_validate(rebar.validate())

    @mcp.tool()
    def get_file_impact(ticket_id: str) -> list[FileImpactItemOut]:
        """Get the file-impact array (consumed by next-batch conflict scheduling)."""
        return [FileImpactItemOut.model_validate(e) for e in rebar.get_file_impact(ticket_id)]

    @mcp.tool()
    def get_verify_commands(ticket_id: str) -> list[VerifyCommandItemOut]:
        """Get the DD-level verify-commands array for a ticket."""
        return [VerifyCommandItemOut.model_validate(e) for e in rebar.get_verify_commands(ticket_id)]

    @mcp.tool()
    def summary(ticket_ids: list[str]) -> list[dict]:
        """One-line-per-ticket summary [{ticket_id, status, title, blocking_summary}]."""
        return rebar.summary(*ticket_ids)

    @mcp.tool()
    def list_epics(
        include_blocked: bool = False,
        has_tag: str | None = None,
        min_children: int | None = None,
    ) -> ListEpicsOut:
        """DEPRECATED — thin wrapper over `list`. Returns {p0_bugs, epics} (ticket_state
        arrays) from two generic calls. Prefer the `list_tickets` tool directly:
        ticket_type='epic', status='open,in_progress', blocking_state='unblocked',
        min_children=N — plus ticket_type='bug', priority=0 for the P0 bugs.
        include_blocked=True drops the unblocked-only filter."""
        import warnings

        with warnings.catch_warnings():  # the tool's docstring is the deprecation signal
            warnings.simplefilter("ignore", DeprecationWarning)
            return ListEpicsOut.model_validate(
                rebar.list_epics(
                    include_blocked=include_blocked, has_tag=has_tag, min_children=min_children
                )
            )

    @mcp.tool()
    def bridge_fsck() -> BridgeFsckOut:
        """Audit bridge mappings -> {orphaned, duplicates, stale}."""
        return BridgeFsckOut.model_validate(rebar.bridge_fsck())

    @mcp.tool()
    def verify_signature(ticket_id: str) -> dict:
        """Certify a ticket's verified-steps manifest against its signature.

        Recomputes the HMAC with THIS environment's signing key and returns
        {ticket_id, verified, verdict, reason, manifest, ...}. verdict is
        'certified' (steps match), 'mismatch' (altered/invalid), 'foreign_key'
        (signed by a different environment), or 'unsigned'. Read-only."""
        return rebar.verify_signature(ticket_id)

    @mcp.tool()
    def reconcile(mode: str = "dry-run") -> dict:
        """Run the Jira reconciler. Defaults to a non-mutating dry-run.

        The Jira-mutating modes (bootstrap-strict, bootstrap-throttle, live) each
        require REBAR_MCP_ALLOW_RECONCILE_LIVE=1 and are blocked under
        REBAR_MCP_READONLY. reconcile-check / dry-run are non-mutating.
        """
        # MODE_CAPS / Mode are imported once at module load (see top of file).
        # Unknown mode -> ValueError -> clean tool error.
        parsed = Mode.from_str(mode)
        # Any cap != 0 mutates Jira (10/100/None — note LIVE's cap is None, so we
        # gate on != 0, NOT > 0). cap-0 modes are non-mutating and always allowed.
        if MODE_CAPS[parsed] != 0:
            if _readonly():
                raise ValueError(
                    f"{parsed.value} reconcile is disabled: this server is "
                    "read-only (REBAR_MCP_READONLY)"
                )
            if not _env_truthy("REBAR_MCP_ALLOW_RECONCILE_LIVE"):
                raise ValueError(
                    f"{parsed.value} reconcile is disabled (mutating mode); "
                    "set REBAR_MCP_ALLOW_RECONCILE_LIVE=1 to enable"
                )
        return rebar.reconcile(parsed.value)

    @mcp.tool()
    def review_ticket(
        ticket_id: str, reviewer_id: str | None = None, graph: bool = False
    ) -> dict:
        """Run an LLM review of a ticket (or its graph) -> a review_result dict
        {findings[], target, reviewers, runner, model, trace_id, summary}.

        DISABLED unless REBAR_MCP_ALLOW_LLM=1: this makes a live, billable LLM call
        and reaches the network + filesystem (it is not a plain store read). It
        needs the 'agents' extra + a model API key (provider per REBAR_LLM_MODEL,
        e.g. ANTHROPIC_API_KEY or OPENAI_API_KEY). Returns a plain dict and
        advertises NO outputSchema by design — the result is model-produced, so it
        is a documented NO_SCHEMA_EXEMPT and is not auto-driven in CI."""
        if not _env_truthy("REBAR_MCP_ALLOW_LLM"):
            raise ValueError(
                "review_ticket is disabled: it makes a live, billable LLM call. "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        import rebar.llm

        return rebar.llm.review_ticket(ticket_id, reviewer_id, graph=graph)

    @mcp.tool()
    def review_code(
        base: str = "HEAD~1",
        head: str = "HEAD",
        reviewers: list[str] | None = None,
    ) -> dict:
        """Run a multi-reviewer LLM code review of a git range (base..head) ->
        an aggregated review_result dict (findings carry agreement + reviewers).

        DISABLED unless REBAR_MCP_ALLOW_LLM=1 (live, billable LLM call(s); reaches
        network + filesystem + git). Needs the 'agents' extra + an API key. Returns
        a plain dict and advertises NO outputSchema by design (documented
        NO_SCHEMA_EXEMPT) — its CLI/library --output json is pinned to
        review_result."""
        if not _env_truthy("REBAR_MCP_ALLOW_LLM"):
            raise ValueError(
                "review_code is disabled: it makes live, billable LLM call(s). "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        import rebar.llm

        return rebar.llm.review_code(base=base, head=head, reviewers=reviewers)

    @mcp.tool()
    def scan_spec(spec_text: str, batch_size: int = 5) -> dict:
        """Batch-scan the store's open epics against a specification -> a
        review_result dict (gaps/conflicts/overlaps), epics evaluated in batches.

        DISABLED unless REBAR_MCP_ALLOW_LLM=1 (live, billable LLM call(s)). Needs
        the 'agents' extra + an API key. Returns a plain dict and advertises NO
        outputSchema by design (documented NO_SCHEMA_EXEMPT)."""
        if not _env_truthy("REBAR_MCP_ALLOW_LLM"):
            raise ValueError(
                "scan_spec is disabled: it makes live, billable LLM call(s). "
                "Set REBAR_MCP_ALLOW_LLM=1 to enable it."
            )
        import rebar.llm

        return rebar.llm.scan_epics_for_spec(spec_text, batch_size=batch_size)

    # ── Write tools (gated by REBAR_MCP_READONLY) ──────────────────────────────
    if not _readonly():

        @mcp.tool()
        def create_ticket(
            ticket_type: str,
            title: str,
            parent: str | None = None,
            priority: int | None = None,
            assignee: str | None = None,
            description: str | None = None,
            tags: list[str] | None = None,
        ) -> CreateResultOut:
            """Create a ticket; returns {id, alias} (agents get the alias without
            a second show())."""
            return CreateResultOut.model_validate(
                rebar.create_ticket(
                    ticket_type,
                    title,
                    parent=parent,
                    priority=priority,
                    assignee=assignee,
                    description=description,
                    tags=tags,
                    return_alias=True,
                )
            )

        @mcp.tool()
        def transition_ticket(
            ticket_id: str, current_status: str, target_status: str
        ) -> dict:
            """Transition a ticket's status (optimistic concurrency). Returns the
            engine result {ticket_id, from, to, newly_unblocked}."""
            return rebar.transition(ticket_id, current_status, target_status)

        @mcp.tool()
        def claim_ticket(ticket_id: str, assignee: str | None = None) -> ClaimResultOut:
            """Atomically claim an OPEN ticket (-> in_progress + assignee).

            Raises a tool error (ConcurrencyError) if the ticket is not open —
            i.e. another agent already claimed it.
            """
            return ClaimResultOut.model_validate(rebar.claim(ticket_id, assignee=assignee))

        @mcp.tool()
        def reopen_ticket(ticket_id: str) -> dict:
            """Reopen a closed ticket (closed -> open). Optimistic-concurrency:
            raises a tool error if the ticket is not currently closed."""
            return rebar.reopen(ticket_id)

        @mcp.tool()
        def comment_ticket(ticket_id: str, body: str) -> str:
            """Append a comment to a ticket."""
            rebar.comment(ticket_id, body)
            return "ok"

        @mcp.tool()
        def edit_ticket(
            ticket_id: str,
            title: str | None = None,
            priority: int | None = None,
            assignee: str | None = None,
            description: str | None = None,
            tags: list[str] | None = None,
            ticket_type: str | None = None,
        ) -> str:
            """Edit ticket fields (title/priority/assignee/description/tags/ticket_type)."""
            rebar.edit_ticket(
                ticket_id,
                title=title,
                priority=priority,
                assignee=assignee,
                description=description,
                tags=tags,
                ticket_type=ticket_type,
            )
            return "ok"

        @mcp.tool()
        def link_tickets(id1: str, id2: str, relation: str) -> str:
            """Link two tickets (one of the six canonical relations: blocks |
            depends_on | relates_to | duplicates | supersedes | discovered_from)."""
            rebar.link(id1, id2, relation)
            return "ok"

        @mcp.tool()
        def unlink_tickets(id1: str, id2: str) -> str:
            """Remove a link between two tickets."""
            rebar.unlink(id1, id2)
            return "ok"

        @mcp.tool()
        def tag_ticket(ticket_id: str, tag: str) -> str:
            """Add a tag to a ticket."""
            rebar.tag(ticket_id, tag)
            return "ok"

        @mcp.tool()
        def untag_ticket(ticket_id: str, tag: str) -> str:
            """Remove a tag from a ticket."""
            rebar.untag(ticket_id, tag)
            return "ok"

        @mcp.tool()
        def archive_ticket(ticket_id: str) -> str:
            """Archive a ticket (excludes it from the default list)."""
            rebar.archive(ticket_id)
            return "ok"

        @mcp.tool()
        def compact_ticket(ticket_id: str | None = None) -> str:
            """Compact a ticket's event log (or all tickets if id omitted)."""
            rebar.compact(ticket_id)
            return "ok"

        # ── File-impact / verify-commands writes (WS5d; feed next-batch) ───────
        # Typed item params so the tools advertise an inputSchema (the {path,reason}
        # / {dd_id,dd_text,command} shapes mirror the get_* output models + schemas).
        @mcp.tool()
        def set_file_impact(ticket_id: str, impact: list[FileImpactItemOut]) -> str:
            """Record file impact (list of {path, reason}) for conflict-aware
            next-batch scheduling."""
            rebar.set_file_impact(ticket_id, [_dump(e) for e in impact])
            return "ok"

        @mcp.tool()
        def set_verify_commands(ticket_id: str, commands: list[VerifyCommandItemOut]) -> str:
            """Record DD-level verify commands (list of {dd_id, dd_text, command})."""
            rebar.set_verify_commands(ticket_id, [_dump(e) for e in commands])
            return "ok"

        @mcp.tool()
        def sign_manifest(ticket_id: str, manifest: list[str]) -> dict:
            """Sign a manifest of verified steps with the environment signing key.

            Computes an HMAC-SHA256 over the steps with this environment's key
            (REBAR_SIGNING_KEY or the gitignored .signing-key) and records a
            SIGNATURE event. Returns {ticket_id, manifest, algorithm, signature,
            key_id, head_sha, signed_at}. Use verify_signature to certify it
            later — only this environment (holding the key) can certify."""
            return rebar.sign_manifest(ticket_id, manifest)

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
