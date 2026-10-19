"""The override table for the 48 library-backed MCP tools (ADR 0119).

One :class:`~rebar._mcp_derive.ToolSpec` per tool. ``description`` is the LLM-facing tool text,
carried verbatim (including its continuation indentation) because no library docstring is
written for an MCP client; ``divergence`` carries the reason for every deliberate difference
from the library signature (a stripped or pinned parameter, a narrowed type, a changed
default or order). The 16 MCP-only tools stay hand-written in their registrars. The golden
surface test (``tests/interfaces/facades/test_mcp_tool_surface_golden.py``) pins the result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rebar._mcp_derive import (
    Param,
    ToolSpec,
    ack,
    as_dict,
    as_rows,
    attach_warning,
    bound_list,
    description_warning,
    discovery_rows,
    escalation_text,
    fsck_recover_gate,
    id_alias_only,
    jira_sync_gate,
    plan_review_health,
    readonly_gate,
    readonly_pin,
    warn_after,
    warn_before,
    with_push,
)
from rebar._mcp_schema import pydantic_typed_dict
from rebar.types import FileImpactEntry, VerifyCommandEntry

# MCP inputs typed with the canonical shared entries (common.schema.json), rebuilt for
# pydantic, which refuses ``typing.TypedDict`` on Python < 3.12.
if TYPE_CHECKING:
    _FileImpactEntry = FileImpactEntry
    _VerifyCommandEntry = VerifyCommandEntry
else:
    try:
        _FileImpactEntry = pydantic_typed_dict(FileImpactEntry)
        _VerifyCommandEntry = pydantic_typed_dict(VerifyCommandEntry)
    except ImportError:  # core-only install: build_server refuses before any registration
        _FileImpactEntry = FileImpactEntry
        _VerifyCommandEntry = VerifyCommandEntry

#: The create tools return ``{id, alias}`` and stamp the MCP ingress on the genesis CREATE.
_CREATED_BY_MCP: dict[str, Any] = {"return_alias": True, "_creation_channel": "mcp"}

#: ``**fields`` cannot be a JSON schema, so edit_ticket enumerates the editable fields; a new
#: editable library field must be added here too.
_EDIT_FIELDS: dict[str, Param] = {
    "title": Param(annotation=str | None, default=None),
    "priority": Param(annotation=int | None, default=None),
    "assignee": Param(annotation=str | None, default=None),
    "description": Param(annotation=str | None, default=None),
    "ticket_type": Param(annotation=str | None, default=None),
    "add_tags": Param(annotation=list[str] | None, default=None),
    "remove_tags": Param(annotation=list[str] | None, default=None),
    "set_tags": Param(annotation=list[str] | None, default=None),
    "bridge_project": Param(annotation=str | None, default=None),
    "repos": Param(annotation=list[str] | None, default=None),
}

#: MCP positional order for transition_ticket (``close_class`` before ``force``), kept for
#: existing positional callers (tests/unit/test_cross_session_mcp.py).
_TRANSITION_ORDER = (
    "ticket_id",
    "current_status",
    "target_status",
    "close_class",
    "reason",
    "force",
    "caused_by",
    "ref",
)

TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="archive_ticket",
        library="archive",
        registrar="write",
        annotation="DESTRUCTIVE",
        description="""Archive a ticket (excludes it from the default list).""",
        pre=(warn_before("ticket_id"),),
        post=(ack,),
    ),
    ToolSpec(
        name="attach_commits",
        library="attach_commits",
        registrar="write",
        annotation="MUTATE_IDEMPOTENT",
        description="""Retroactively link commits to a ticket by SHA (union-add, idempotent).

        The repair path when a commit landed without a usable `rebar-ticket:` trailer:
        attaching the SHAs records a COMMITS event so the close gate can still tie the
        ticket to its change. Every SHA must resolve to a commit in this repository —
        validation is ALL-OR-NOTHING, so if any SHA is bad, nothing is recorded.""",
        params={"commits": Param(annotation=list[str])},
        post=(as_dict,),
    ),
    ToolSpec(
        name="bridge_check_access",
        library="bridge_check_access",
        registrar="bridge_read",
        annotation="READ_ONLY_OPEN_WORLD",
        description="""Run the six-step live Jira capability check and return its typed verdict.""",
        post=(as_dict,),
    ),
    ToolSpec(
        name="bridge_fsck",
        library="bridge_fsck",
        registrar="read",
        annotation="READ_ONLY",
        description=(
            "Offline bridge audit -> {unknown_event_types, binding_drift, store_integrity}."
        ),
        post=(as_dict,),
    ),
    ToolSpec(
        name="bridge_pause",
        library="bridge_pause",
        registrar="bridge_mutation",
        annotation="MUTATE_OPEN_WORLD",
        description="""Persist a durable reconciliation pause with its operator reason.""",
        pre=(readonly_gate("bridge pause"), jira_sync_gate("bridge pause")),
        post=(as_dict,),
    ),
    ToolSpec(
        name="bridge_preview",
        library="bridge_preview",
        registrar="bridge_read",
        annotation="READ_ONLY",
        description="""Compute proposed Jira changes without applying them.""",
        post=(as_dict,),
        omit_none=True,
    ),
    ToolSpec(
        name="bridge_projects_list",
        library="bridge_projects_list",
        registrar="read",
        annotation="READ_ONLY",
        description="""Read the store's ``{project: {"repos": [...]}}`` sync mapping without an LLM.

        Its project keys are the sync list; each value names that project's repositories.
        """,
    ),
    ToolSpec(
        name="bridge_projects_remove",
        library="bridge_projects_remove",
        registrar="write",
        annotation="MUTATE",
        description="""Remove a bridge project key from the mapping (error if absent).""",
        post=(ack,),
    ),
    ToolSpec(
        name="bridge_projects_set",
        library="bridge_projects_set",
        registrar="write",
        annotation="MUTATE_IDEMPOTENT",
        description="""Set a bridge project key's repos (REPLACE semantics; idempotent).""",
        post=(ack,),
    ),
    ToolSpec(
        name="bridge_resume",
        library="bridge_resume",
        registrar="bridge_mutation",
        annotation="MUTATE_OPEN_WORLD",
        description="""Clear the durable reconciliation pause.""",
        pre=(readonly_gate("bridge resume"), jira_sync_gate("bridge resume")),
        post=(as_dict,),
    ),
    ToolSpec(
        name="bridge_run",
        library="bridge_run",
        registrar="bridge_mutation",
        annotation="MUTATE_OPEN_WORLD",
        description="""Run one scheduled bridge profile and strictly deliver its ticket events.""",
        params={"profile": Param(annotation=str, default="dry-run")},
        pre=(readonly_gate("bridge run"), jira_sync_gate("bridge run")),
        post=(as_dict,),
    ),
    ToolSpec(
        name="bridge_status",
        library="bridge_status",
        registrar="bridge_read",
        annotation="READ_ONLY",
        description="""Read the durable bridge status snapshot and optional freshness assertion.""",
        post=(as_dict,),
        omit_none=True,
    ),
    ToolSpec(
        name="bridge_sync",
        library="bridge_sync",
        registrar="bridge_mutation",
        annotation="MUTATE_OPEN_WORLD",
        description="""Apply proposed Jira changes, optionally with an explicit change limit.""",
        pre=(readonly_gate("bridge sync"), jira_sync_gate("bridge sync")),
        post=(as_dict,),
        omit_none=True,
    ),
    ToolSpec(
        name="check_ac",
        library="check_ac",
        registrar="read",
        annotation="READ_ONLY",
        description="""Check the ticket has an Acceptance Criteria block
        ({verdict, criteria_count, reason, passed}).""",
        post=(as_dict, warn_after("ticket_id")),
    ),
    ToolSpec(
        name="claim_ticket",
        library="claim",
        registrar="write",
        annotation="MUTATE",
        description="""Atomically claim an OPEN ticket (-> in_progress + assignee).

        Raises a tool error (ConcurrencyError) if the ticket is not open —
        i.e. another agent already claimed it. ``force`` supplies the reason for
        bypassing the plan-review gate; when it replaces certification, the absent
        certified attestation is the durable audit signal.
        """,
        post=(with_push,),
    ),
    ToolSpec(
        name="clarity_check",
        library="clarity_check",
        registrar="read",
        annotation="READ_ONLY",
        description="""Score ticket clarity (score / verdict / threshold / passed).""",
        post=(as_dict,),
    ),
    ToolSpec(
        name="comment_ticket",
        library="comment",
        registrar="write",
        annotation="MUTATE",
        description="""Append a comment to a ticket.""",
        strip=("source", "allow_secret_pattern"),
        pre=(warn_before("ticket_id"),),
        post=(ack,),
        divergence=(
            "`allow_secret_pattern` is the audited force override for the write-time secret "
            "screen (bug e7a9): a non-empty reason lets a body the screen refused through. "
            "rebar.comment's own docstring (src/rebar/_lib_mutations.py:56) states it is "
            '"Deliberately not exposed over MCP" — an MCP caller must not be able to suppress'
            " the secret guard. `source` is the P1.2 per-comment import provenance "
            "(source_author / source_created_at), used by importers, not by an agent "
            "commenting through a tool."
        ),
    ),
    ToolSpec(
        name="compact_ticket",
        library="compact",
        registrar="write",
        annotation="DESTRUCTIVE",
        description="""Compact a ticket's event log (or all tickets if id omitted).""",
        post=(ack,),
    ),
    ToolSpec(
        name="create_idea",
        library="idea",
        registrar="write",
        annotation="MUTATE",
        description="""Capture an undesigned idea: create an epic in status 'idea' atomically.

        The idea is born in status 'idea' via a single CREATE event (never momentarily
        'open'/claimable), is excluded from ready/next-batch, and 'idea -> closed'
        (reject) skips the completion gates. Promote a kept idea with
        transition_ticket(id, "idea", "open"). Returns {id, alias}.""",
        pins=_CREATED_BY_MCP,
        post=(with_push,),
        divergence=(
            'The tool pins the library\'s `return_alias=True` and `_creation_channel="mcp"` at'
            " the call site (src/rebar/_mcp_writes.py, the `rebar.idea(...)` call; library at"
            " src/rebar/_lib_writes.py:193), so neither is caller-settable: MCP always "
            "returns {id, alias} and always stamps the mcp creation channel. Same shape as "
            "`create_ticket` and `create_identity`."
        ),
    ),
    ToolSpec(
        name="create_identity",
        library="create_identity",
        registrar="write",
        annotation="MUTATE",
        description="""Create an identity entity: a gate-/graph-exempt ticket recording a
        person/agent. ``name`` is the title; ``email`` plus ``mappings`` (list of
        {provider, external_id}) and ``keys`` (OpenSSH authorized-keys lines) ride the
        CREATE and surface in show_ticket. Returns {id, alias}.""",
        strip=("tags",),
        pins=_CREATED_BY_MCP,
        post=(with_push,),
        divergence=(
            'The tool pins the library\'s `return_alias=True` and `_creation_channel="mcp"` at'
            " the call site (src/rebar/_mcp_writes.py), so neither is caller-settable: MCP "
            "always returns {id, alias} and always stamps the mcp creation channel. `tags` is"
            " not exposed on identity creation over MCP; tags are applied afterwards with the"
            " `tag_ticket` tool."
        ),
    ),
    ToolSpec(
        name="create_ticket",
        library="create_ticket",
        registrar="write",
        annotation="MUTATE",
        description="""Create a ticket; returns {id, alias} (agents get the alias without
        a second show()). A non-null description_warning means the description exceeds
        the plan-review admission cap while the claim gate is on — the ticket was still
        created, but claiming it needs a review that refuses the description as-is. A
        non-null duplicate_warning means another ticket with the same normalized title
        was created inside the recency window — advisory only; the named candidate may
        already cover this work.""",
        strip=("source",),
        pins=_CREATED_BY_MCP,
        post=(with_push,),
        divergence=(
            'The tool pins the library\'s `return_alias=True` and `_creation_channel="mcp"` at'
            " the call site (src/rebar/_mcp_writes.py), so neither is caller-settable. "
            "`source` is the provenance dict used by importers/bridge paths, not part of the "
            "agent-facing tool surface."
        ),
    ),
    ToolSpec(
        name="declare_no_file_impact",
        library="declare_no_file_impact",
        registrar="write",
        annotation="MUTATE_IDEMPOTENT",
        description="""Declare that a ticket has no repository-file impact, with a reason.""",
        pre=(warn_before("ticket_id"),),
        post=(ack,),
    ),
    ToolSpec(
        name="edit_ticket",
        library="edit_ticket",
        registrar="write",
        annotation="MUTATE",
        description="""Edit ticket fields (title/priority/assignee/description/ticket_type).

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
        """,
        params=_EDIT_FIELDS,
        pre=(warn_before("ticket_id"),),
        post=(description_warning, ack),
        divergence=(
            "The library is `edit_ticket(ticket_id, *, repo_root=None, **fields)` "
            "(src/rebar/_lib_mutations.py); a **fields passthrough cannot be expressed as a "
            "JSON tool schema, so the tool ENUMERATES the eleven editable fields and forwards"
            " them explicitly. The tool's own docstring documents this "
            '(src/rebar/_mcp_writes.py: "Unlike the library edit_ticket (a **fields '
            "passthrough), this tool's signature is enumerated\"). The enumerated set is the "
            "divergence to watch: a new editable library field must be added here too."
        ),
    ),
    ToolSpec(
        name="fsck",
        library="fsck_report",
        registrar="read",
        annotation="MUTATE_IDEMPOTENT",
        description="""Check ticket-store integrity (JSON validity, CREATE presence, lock
        cleanup). Set recover=True to run the recovery path.""",
        pins={"report_only": readonly_pin()},
        pre=(fsck_recover_gate,),
        post=(as_dict,),
        divergence=(
            "The tool derives `report_only` from the server's own read-only gate "
            "(`rebar.fsck_report(recover=recover, report_only=_readonly())`) rather than "
            "accepting it from the caller, so a read-only server cannot be talked into the "
            "stale-lock write."
        ),
    ),
    ToolSpec(
        name="get_file_impact",
        library="get_file_impact",
        registrar="read",
        annotation="READ_ONLY",
        description="""Get the file-impact array (consumed by next-batch conflict scheduling).""",
        post=(as_rows,),
    ),
    ToolSpec(
        name="get_verify_commands",
        library="get_verify_commands",
        registrar="read",
        annotation="READ_ONLY",
        description="""Get the DD-level verify-commands array for a ticket.""",
        post=(as_rows,),
    ),
    ToolSpec(
        name="grounding_info",
        library="grounding_info",
        registrar="read",
        annotation="READ_ONLY",
        description="""The STATIC code-grounding oracle integration contract (epic 8f6c): the
        closed dimension-ID vocabulary + version, the reference kinds, the closed
        abstain-reason enum (+ outcome/job/tier vocabularies), and the available
        backends with their detected availability/version. A fast, deterministic,
        repo-independent discovery surface (no repo is scanned). Takes no args.""",
        post=(as_dict,),
    ),
    ToolSpec(
        name="link_tickets",
        library="link",
        registrar="write",
        annotation="MUTATE",
        description="""Link two tickets (one of the seven canonical relations: blocks |
        depends_on | relates_to | duplicates | supersedes | discovered_from |
        caused_by).

        Blocking relations are escalated to comparable endpoints when the two tickets
        do not share a parent, so the RECORDED edge may differ from the requested one.
        When that happens the return value names both pairs — otherwise the caller
        would be told "ok" for an edge that was never written.

        A ``caused_by`` link whose target has no commit referencing it is refused;
        ``force`` (a reason string) bypasses that check.
        """,
        pre=(warn_before("id1"),),
        post=(escalation_text, ack),
    ),
    ToolSpec(
        name="list_tickets",
        library="list_tickets",
        registrar="read",
        annotation="READ_ONLY",
        description="""List tickets with optional lifecycle, hierarchy, tag, and readiness filters.

        Archived exclusion already hides deleted tombstones; ``exclude_deleted`` matters
        with ``include_archived=True``. Rows carry ``children_count`` and can require a
        minimum. The default omits bodies and signature material; ``full=True`` restores
        complete state, while ``show_ticket`` remains the single-ticket alternative.
        Oversize results fail with structured ``response_too_large`` details and narrowing
        filters; they are never truncated or dropped by the transport.
        """,
        params={"priority": Param(annotation=int | None), "full": Param(default=False)},
        post=(as_rows, bound_list("list_tickets")),
    ),
    ToolSpec(
        name="log_session",
        library="append_session_log",
        registrar="write",
        annotation="MUTATE",
        description="""Append a verbose entry to the current session_log, creating one on
        first use (write-gated: refused under REBAR_MCP_READONLY=1). Returns the
        log's {id, alias}; optional relates_to / discovered_from link it to the
        work it documents.""",
        pins={"_creation_channel": "mcp"},
        post=(id_alias_only, with_push),
        divergence=(
            'The tool pins the library\'s `_creation_channel="mcp"` at the call site '
            "(src/rebar/_mcp_writes.py, the `rebar.append_session_log(...)` call; library at "
            "src/rebar/_lib_mutations.py:84) so the provenance of an MCP-written session log "
            "cannot be spoofed by the caller. Every other library parameter is exposed."
        ),
    ),
    ToolSpec(
        name="next_batch",
        library="next_batch",
        registrar="read",
        annotation="READ_ONLY",
        description="""Next parallel batch of unblocked tickets under an epic's hierarchy.""",
        post=(as_dict,),
    ),
    ToolSpec(
        name="quality_check",
        library="quality_check",
        registrar="read",
        annotation="READ_ONLY",
        description="""Check ticket dispatch readiness ({verdict, line_count, keyword_count,
        ac_items, file_impact, reason, passed}).""",
        post=(as_dict, warn_after("ticket_id")),
    ),
    ToolSpec(
        name="ready_tickets",
        library="ready",
        registrar="read",
        annotation="READ_ONLY",
        description="""List tickets whose blockers are closed, optionally sorted.

        The lean default returns id, alias, title, type, status, priority, and blocking
        summary; ``full=True`` restores the pre-1.0 full ``ticket_state`` contract, or use
        ``show_ticket`` for one item. See ``docs/release-notes.md``. Like ``list_tickets``,
        oversize results are refused; because this tool has no filters, the remedy names
        ``next_batch(epic_id)``. Sort keys are ``priority|created|updated|id|status``;
        prefix ``-`` for descending, with unset values last.
        """,
        params={"full": Param(annotation=bool, default=False, extra=True)},
        post=(discovery_rows, bound_list("ready_tickets")),
        divergence=(
            "`full` is implemented in the MCP layer, not the library: the tool calls "
            "`_discovery_rows(rebar.ready(sort=sort), full=full)` and projects the rows "
            "itself. That is deliberate and documented at the seam — `_discovery_rows` "
            '(src/rebar/_mcp_reads.py) states that "`ready_tickets` has no such flag on the '
            "library surface -- `rebar.ready` -> `ready_states` -> `public_state` carries "
            'none and this change deliberately adds none -- so it projects here", through the'
            " same `lean_projection` `list_tickets` uses so the two discovery surfaces cannot"
            " return different shapes (story 98b8-5f08-1569-45cc). The flag exists to keep "
            "the response inside the MCP payload budget, a transport concern the library does"
            " not have."
        ),
    ),
    ToolSpec(
        name="recent_session_logs",
        library="recent_session_logs",
        registrar="read",
        annotation="READ_ONLY",
        description="""The newest session_log tickets, newest first (by created_at; default
        limit 5). session_logs are hidden from list_tickets; this is the
        type-specific read that surfaces them.""",
        post=(as_rows,),
    ),
    ToolSpec(
        name="reopen_ticket",
        library="reopen",
        registrar="write",
        annotation="MUTATE",
        description="""Reopen a closed ticket (closed -> open). Optimistic-concurrency:
        raises a tool error if the ticket is not currently closed.""",
        pre=(warn_before("ticket_id"),),
        post=(with_push, attach_warning),
    ),
    ToolSpec(
        name="search",
        library="search",
        registrar="read",
        annotation="READ_ONLY",
        description="""Search ticket prose and tags with bounded discovery results.

        Queries support ``status:``, ``type:``, ``priority:``, ``assignee:``, ``tag:``,
        and ``parent:``; commas mean field-local OR, priorities accept comparisons and
        ``n..m``, and ``-``/``not:`` negate. Unknown fields become literal text. Sort by
        ``priority|created|updated|id|status``; prefix ``-`` for descending, with unset
        values last. Oversize results return ``response_too_large`` with count, size,
        and narrowing guidance rather than truncating.
        """,
        pins={"full": False},
        post=(as_rows, bound_list("search")),
        divergence=(
            "The tool always uses the projected row shape (library default `full=False`); "
            "`full` is not caller-settable because the projection is what keeps the response "
            "inside the MCP payload budget (bug 494b-2dd3-e9d3-4fb0)."
        ),
    ),
    ToolSpec(
        name="set_file_impact",
        library="set_file_impact",
        registrar="write",
        annotation="MUTATE_IDEMPOTENT",
        description="""Record file impact (list of {path, reason}) for conflict-aware
        next-batch scheduling.""",
        params={"impact": Param(annotation=list[_FileImpactEntry])},
        pre=(warn_before("ticket_id"),),
        post=(ack,),
    ),
    ToolSpec(
        name="set_verify_commands",
        library="set_verify_commands",
        registrar="write",
        annotation="MUTATE_IDEMPOTENT",
        description="""Record DD-level verify commands (list of {dd_id, dd_text, command}).""",
        params={"commands": Param(annotation=list[_VerifyCommandEntry])},
        pre=(warn_before("ticket_id"),),
        post=(ack,),
    ),
    ToolSpec(
        name="show_ticket",
        library="show_ticket",
        registrar="read",
        annotation="READ_ONLY",
        description="""Show compiled ticket state (accepts full id, short id, or alias).
        Includes the computed ``inbound_deps`` (inbound edges: other tickets
        linking TO this one, with the source's status) alongside the stored
        outgoing ``deps``.""",
        pins={"include_inbound": True},
        post=(as_dict, plan_review_health, warn_after("ticket_id")),
        divergence=(
            "The tool always passes `include_inbound=True` so the computed `inbound_deps` are"
            " part of every MCP ticket read; there is no reduced mode for a caller to select."
        ),
    ),
    ToolSpec(
        name="sign_manifest",
        library="sign_manifest",
        registrar="write",
        annotation="MUTATE",
        description="""Sign a manifest of verified steps as an asymmetric op-cert.

        Mints a rebar.opcert.v1 DSSE op-cert over the steps with this
        environment's Ed25519 key (the gitignored .opcert-key) and records a
        SIGNATURE event. Returns {ticket_id, manifest, algorithm:'sshsig',
        envelope, principal, material_fingerprint, merged_log_commit,
        head_sha, signed_at}. The op-cert kinds (plan-review /
        completion-verifier) are signed and accepted ONLY as op-certs — the
        legacy symmetric HMAC scheme is retired for them (story 8f1d). Use
        verify_signature to certify it later.""",
        params={"manifest": Param(annotation=list[str])},
        post=(with_push,),
    ),
    ToolSpec(
        name="summary",
        library="summary",
        registrar="read",
        annotation="READ_ONLY",
        description="One-line-per-ticket summary "
        """[{ticket_id, alias, status, title, blocking_summary}].

        ticket_id preserves the caller token; alias is the exact resolved
        human-friendly alias, or null when resolution fails closed.
        """,
    ),
    ToolSpec(
        name="tag_ticket",
        library="tag",
        registrar="write",
        annotation="MUTATE_IDEMPOTENT",
        description="""Add a tag to a ticket.""",
        pre=(warn_before("ticket_id"),),
        post=(ack,),
    ),
    ToolSpec(
        name="ticket_deps",
        library="deps",
        registrar="read",
        annotation="READ_ONLY",
        description="""Show the dependency graph for a ticket.""",
        post=(as_dict,),
    ),
    ToolSpec(
        name="transition_ticket",
        library="transition",
        registrar="write",
        annotation="MUTATE",
        description="""Transition a ticket's status (optimistic concurrency). Returns the
        engine result {ticket_id, from, to, newly_unblocked}.

        ``open -> in_progress`` starts work and is gated by the plan-review gate
        (``verify.require_plan_review_for_claim``) exactly like ``claim_ticket``.
        ``force`` supplies the operator's audit reason while bypassing whichever gate
        applies to this transition: the start-work gate or the completion-close gate.
        Only ``null`` means no bypass; an empty string is a present bypass and the
        shared core records its audit-safe placeholder. When force replaces otherwise-
        required certification, the absent certified attestation is the durable audit
        signal.

        ``close_class`` is REQUIRED when closing a ``bug`` ticket. Bug closes accept
        the full bounded vocabulary: ``regression``, ``plan_defect``,
        ``env_integration``, ``flaky``, ``preexisting``, ``not_a_bug``,
        ``duplicate``, ``escalated``, ``obsolete``, ``superseded``, ``wontfix``,
        ``undetermined``. A non-bug ticket normally closes without ``close_class``;
        only the administrative subset may be supplied: ``duplicate``, ``obsolete``,
        ``superseded``, ``wontfix``. It is ignored for non-closing transitions. Force
        does not relax this classification invariant: a bug close without a valid
        ``close_class`` is refused.

        ``reason`` is the close reason for a reason-required ``close_class``
        (``obsolete``/``wontfix``, and — absent a live replacement link —
        ``not_a_bug``/``escalated``): it is recorded as the disposition's
        ``close_reason`` and signed into the attestation. It is NOT the force
        bypass note (that is ``force``'s value) and is discarded on any other
        combination, exactly as on the CLI's ``--reason``.

        ``caused_by`` records the explicit culprit for a bug close. ``ref`` selects
        the committed tree used by completion verification; it defaults to HEAD.""",
        order=_TRANSITION_ORDER,
        pre=(warn_before("ticket_id"),),
        post=(with_push, attach_warning),
    ),
    ToolSpec(
        name="unlink_tickets",
        library="unlink",
        registrar="write",
        annotation="MUTATE",
        description="""Remove a link between two tickets, optionally selecting its relation.

        Pass ``relation`` to remove that specific relation while preserving any
        other active relation between the pair. When omitted, removes the pair's
        most-recent active relation (the legacy pair-scoped behavior).
        """,
        pre=(warn_before("id1"),),
        post=(ack,),
    ),
    ToolSpec(
        name="untag_ticket",
        library="untag",
        registrar="write",
        annotation="MUTATE_IDEMPOTENT",
        description="""Remove a tag from a ticket.""",
        pre=(warn_before("ticket_id"),),
        post=(ack,),
    ),
    ToolSpec(
        name="validate",
        library="validate",
        registrar="read",
        annotation="READ_ONLY",
        description="""Repo-wide quality health check (JSON report: score, critical/major/
        minor issues, warnings, suggestions). Takes no ticket id.""",
        post=(as_dict,),
    ),
    ToolSpec(
        name="verify_signature",
        library="verify_signature",
        registrar="read",
        annotation="READ_ONLY",
        description="""Verify a ticket manifest against its op-cert signature.

        Returns ``{ticket_id, verified, verdict, reason, manifest, ...}``: ``certified``
        matches, ``mismatch`` is altered or invalid, ``foreign_key`` lacks an allowed
        key, and ``unsigned`` lacks an op-cert (a retired legacy HMAC record
        included). Outside the opt-in environment
        restriction, signer environment alone is not a gate; ``trust_basis`` names the
        accepted key. Omitted ``kind`` checks the latest signature; an explicit
        ``plan-review`` or ``completion-verifier`` kind is strict. ``show_ticket``
        exposes every attestation.
        """,
        post=(as_dict,),
    ),
)
