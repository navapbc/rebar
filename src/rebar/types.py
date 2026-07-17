"""Typed return contract for the public ``rebar.*`` facade — GENERATED.

DO NOT EDIT BY HAND. Regenerate with::

    python -m rebar.schemas.gen_types

These ``TypedDict``s are derived from the canonical JSON Schemas in
``rebar/schemas/*.schema.json`` and name the *guaranteed* keys of each
return shape. The runtime dicts are OPEN (``additionalProperties: true``):
extra keys may appear as the event-sourced shape evolves, so reading a key
not named here is outside the typed contract by design. Required schema keys
are normal fields; optional keys are ``NotRequired[...]``.
"""

# NOTE: deliberately NO `from __future__ import annotations` — stringized
# annotations hide `NotRequired` from TypedDict, breaking __required_keys__.
# Every type here is defined before use and valid at runtime on Python >=3.11.
from typing import Any, Literal, NotRequired, TypedDict

# --- shared enums (common.schema.json) ---
TicketStatus = Literal["idea", "open", "in_progress", "blocked", "closed", "archived", "deleted"]
TicketType = Literal["bug", "epic", "story", "task", "session_log", "code_review", "identity"]
Relation = Literal[
    "blocks", "depends_on", "relates_to", "duplicates", "supersedes", "discovered_from"
]
CreationChannel = Literal["cli", "mcp", "python", "jira", "import", "unknown"]


# --- shared objects (common.schema.json) ---
class Comment(TypedDict):
    """Shared `comment` object (common.schema.json)."""

    body: str
    author: NotRequired[str | None]
    timestamp: NotRequired[int | str | None]
    source_author: NotRequired[str | None]
    source_created_at: NotRequired[int | str | None]


class Dep(TypedDict):
    """Shared `dep` object (common.schema.json)."""

    target_id: str
    relation: Relation
    link_uuid: NotRequired[str]


class FileImpactEntry(TypedDict):
    """Shared `file_impact_entry` object (common.schema.json)."""

    path: str
    reason: NotRequired[str]


class VerifyCommandEntry(TypedDict):
    """Shared `verify_command_entry` object (common.schema.json)."""

    dd_id: NotRequired[str]
    dd_text: NotRequired[str]
    command: str


class BatchItem(TypedDict):
    """Shared `batch_item` object (common.schema.json)."""

    id: str
    title: str
    priority: NotRequired[int]
    type: NotRequired[TicketType]
    files: NotRequired[list[Any]]
    files_likely_read: NotRequired[list[Any]]


class SkippedItem(TypedDict):
    """Shared `skipped_item` object (common.schema.json)."""

    id: str
    title: str
    conflict_file: NotRequired[str]
    conflict_with: NotRequired[str]
    blocked_story: NotRequired[str]


# --- public return shapes ---
class TicketState(TypedDict):
    """Return shape of the `ticket_state` output schema."""

    ticket_id: str
    ticket_type: TicketType
    title: str
    status: TicketStatus
    priority: int
    tags: list[str]
    assignee: NotRequired[str | None]
    claimed_session: NotRequired[str | None]
    claim_harness: NotRequired[str | None]
    claim_remote_session: NotRequired[str | None]
    parent_id: NotRequired[str | None]
    alias: NotRequired[str | None]
    description: NotRequired[str | None]
    author: NotRequired[str | None]
    created_at: NotRequired[int | None]
    env_id: NotRequired[str | None]
    comments: NotRequired[list[Comment]]
    deps: NotRequired[list[Dep]]
    file_impact: NotRequired[list[FileImpactEntry]]
    verify_commands: NotRequired[list[VerifyCommandEntry]]
    bridge_alerts: NotRequired[list[Any]]
    reverts: NotRequired[list[Any]]
    attestations: NotRequired[dict[str, Any]]
    last_reopened_at: NotRequired[int | None]
    preconditions_summary: NotRequired[dict[str, Any]]
    source_id: NotRequired[str | None]
    source_created_at: NotRequired[int | None]
    source_author: NotRequired[str | None]
    source_env: NotRequired[str | None]
    creation_channel: NotRequired[CreationChannel]
    creation_channel_inferred: NotRequired[Literal[True]]


class TicketStateLLM(TypedDict):
    """Return shape of the `ticket_state_llm` output schema."""

    id: str
    t: TicketType
    ttl: str
    st: TicketStatus
    au: NotRequired[str | None]
    pr: NotRequired[int]
    a: NotRequired[str | None]
    asn: NotRequired[str | None]
    csn: NotRequired[str | None]
    chn: NotRequired[str | None]
    rsn: NotRequired[str | None]
    pid: NotRequired[str | None]
    desc: NotRequired[str]
    cm: NotRequired[list[Any]]
    dp: NotRequired[list[Any]]
    tg: NotRequired[list[Any]]
    ch: NotRequired[list[Any]]
    ibl: NotRequired[list[Any]]


class CreateResult(TypedDict):
    """Return shape of the `create_result` output schema."""

    id: str
    alias: NotRequired[str | None]
    title: NotRequired[str]


class ClaimResult(TypedDict):
    """Return shape of the `claim_result` output schema."""

    ticket_id: str
    status: TicketStatus
    assignee: NotRequired[str | None]


# Return shape of the `transition_result` output schema.
TransitionResult = TypedDict(
    "TransitionResult",
    {
        "ticket_id": str,
        "from": TicketStatus,
        "to": TicketStatus,
        "newly_unblocked": list[str],
    },
)


class ClarityResult(TypedDict):
    """Return shape of the `clarity_result` output schema."""

    score: int
    verdict: Literal["pass", "fail"]
    threshold: int
    passed: NotRequired[bool]


class GateResult(TypedDict):
    """Return shape of the `gate_result` output schema."""

    verdict: Literal["pass", "fail"]
    reason: str
    passed: NotRequired[bool]
    criteria_count: NotRequired[int]
    line_count: NotRequired[int]
    keyword_count: NotRequired[int]
    ac_items: NotRequired[int]
    file_impact: NotRequired[int]


class ValidateReport(TypedDict):
    """Return shape of the `validate_report` output schema."""

    score: int
    critical_issues: list[Any]
    major_issues: list[Any]
    minor_issues: list[Any]
    warnings: list[Any]
    suggestions: list[Any]


class GroundingInfo(TypedDict):
    """Return shape of the `grounding_info` output schema."""

    dimensions_version: int
    dimensions: list[str]
    reference_kinds: list[str]
    abstain_reasons: list[str]
    outcomes: list[str]
    jobs: list[str]
    provenance_tiers: list[str]
    backends: list[dict[str, Any]]


class SignResult(TypedDict):
    """Return shape of the `sign_result` output schema."""

    manifest: list[str]
    algorithm: str
    signed_at: int
    ticket_id: str
    envelope: str
    principal: str
    material_fingerprint: NotRequired[str | None]
    merged_log_commit: NotRequired[str | None]
    head_sha: NotRequired[str | None]
    signature: NotRequired[str | None]
    key_id: NotRequired[str | None]


class VerifySignatureResult(TypedDict):
    """Return shape of the `verify_signature_result` output schema."""

    manifest: list[str]
    step_count: int
    algorithm: str | None
    key_id: str | None
    signed_at: int | None
    head_sha: str | None
    verified: bool
    verdict: Literal[
        "unsigned",
        "foreign_key",
        "certified",
        "mismatch",
        "key_not_valid_at_era",
        "invalid",
        "unavailable",
        "unknown_kind",
        "unknown_scheme",
    ]
    reason: str
    ticket_id: str
    rebar_version: NotRequired[str | None]


class DepsGraph(TypedDict):
    """Return shape of the `deps_graph` output schema."""

    ticket_id: str
    deps: list[Dep]
    blockers: list[str]
    children: list[str]
    ready_to_work: bool


class NextBatch(TypedDict):
    """Return shape of the `next_batch` output schema."""

    epic_id: str
    epic_title: NotRequired[str]
    batch_size: NotRequired[int]
    available_pool: NotRequired[int]
    batch: NotRequired[list[BatchItem]]
    tasks: NotRequired[list[BatchItem]]
    skipped_overlap: NotRequired[list[SkippedItem]]
    skipped_blocked_story: NotRequired[list[SkippedItem]]
    skipped_design_awaiting: NotRequired[list[SkippedItem]]
    skipped_manual_awaiting: NotRequired[list[SkippedItem]]
    skipped_in_progress: NotRequired[list[SkippedItem]]
    skipped_needs_planning: NotRequired[list[SkippedItem]]


class BridgeFsck(TypedDict):
    """Return shape of the `bridge_fsck` output schema."""

    orphaned: list[dict[str, Any]]
    duplicates: list[dict[str, Any]]
    stale: list[dict[str, Any]]
    binding_drift: NotRequired[dict[str, Any]]


class WorkflowRun(TypedDict):
    """Return shape of the `workflow_run` output schema."""

    run_id: str
    ticket_id: NotRequired[str | None]
    workflow_name: NotRequired[str | None]
    status: str
    terminal_step: NotRequired[str | None]
    terminal_output: NotRequired[Any | None]
    error: NotRequired[str | None]
    steps: NotRequired[dict[str, Any]]
    outputs: NotRequired[dict[str, Any]]
    truncated: NotRequired[bool]


# --- public list return shapes ---
# list form of the `file_impact` output schema
FileImpact = list[FileImpactEntry]

# list form of the `verify_commands` output schema
VerifyCommands = list[VerifyCommandEntry]

# list form of the `summary` output schema
Summary = list[dict[str, Any]]
