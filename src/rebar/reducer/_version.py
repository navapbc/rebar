"""Event-schema version + the canonical set of event types this rebar understands.

The event log is the **wire format between clones running different rebar
versions** (see docs/event-schema.md): clones share one ``origin/tickets`` and
merge each other's event files as a union. ``SCHEMA_VERSION`` declares the version
of that wire format.

Forward-compatibility rule (no version negotiation, no VERSION event): an event
whose ``event_type`` is **not** in ``KNOWN_EVENT_TYPES`` is unknown to this
version and is *preserved-and-ignored*:

  * **ignored** at the state level — the reducer skips it without error, so the
    ticket stays fully readable (``_processors.replay``);
  * **preserved** at the file level — compaction must never absorb it into a
    SNAPSHOT or delete its file (``ticket-compact.sh``), so a newer clone's events
    survive a round-trip through an older clone that does not understand them.

``KNOWN_EVENT_TYPES`` is the single source of truth for that set; the reducer's
processor dispatch and the compaction preserve-filter both key off it.

Two DISTINCT compatibility mechanisms — do not conflate them (story 21dd):

  * **Event-schema compatibility** (this module): OPTIONAL-ADDITIVE, forward-compat,
    *preserve-and-ignore*. A newer clone's unknown event *type* is tolerated — the
    file survives, its effect is simply invisible until the older clone upgrades.
    This is deliberately permissive so clones on different versions interoperate.
  * **Store-format compatibility** (``rebar._store.compat`` + the committed
    ``.store-compat.json``): FAIL-CLOSED. A store whose ``format_version`` /
    ``required_capabilities`` this binary cannot interpret blocks every mutating /
    externally-publishing operation before any side effect (reads stay available). An
    ABSENT record is implicit-legacy (version 0) and passes through.

The event log tolerates forward drift; the store format refuses it. ``SCHEMA_VERSION``
governs only the former.
"""

from __future__ import annotations

# Bump when the event wire format changes in a way other clones must be aware of.
# v2 (P2.1): the ``${timestamp}`` filename-prefix is now a single-integer Hybrid
# Logical Clock value (``max(cache, witnessed max-prefix, time_ns()) + 1``) rather
# than a raw ``time.time_ns()``. Same width (19 digits until ~2286) and same
# single-integer encoding, so older clones still string-compare correctly; the only
# *semantic* change is skew-immune causal ordering. No event-body change, so the
# unknown-type forward-compat machinery is not engaged.
# v3 (P2.3): the new ``TAG_DELTA`` event body carries add/remove tag deltas so
# concurrent tag edits converge (no whole-field clobber). This is the FIRST bump
# that adds a new event *body* type, so it DOES engage the unknown-type forward-
# compat path: an older (v2) clone preserves-and-ignores ``TAG_DELTA`` (the file
# survives, the mutation is invisible until it upgrades). The integer itself is
# declarative only — forward-compat is governed by ``KNOWN_EVENT_TYPES`` below, not
# by this value; nothing gates behavior on it.
# v4 (epic gnu-whale-ichor / e165): the new ``KEY_ADD`` / ``KEY_REVOKE`` event bodies
# fold an identity's signed key LIFECYCLE (TOFU genesis, signed add/revoke) into an
# epoch-scoped keyring. Like TAG_DELTA (v3) this adds new event *body* types, so it
# engages the unknown-type forward-compat path: an older (v3) clone preserves-and-ignores
# a ``KEY_ADD``/``KEY_REVOKE`` (the file survives, the keyring mutation is invisible until
# it upgrades). The integer is declarative only — forward-compat is governed by
# ``KNOWN_EVENT_TYPES`` below.
# v5 (epic gnu-whale-ichor): the keyring becomes POSITION-based — each record is
# ``{public_key, added_at, revoked_at}`` where a position is the event's
# ``{timestamp}-{uuid}`` filename prefix (an immutable anchor a verifier resolves to the
# introducing tickets-branch commit), replacing the author-assignable ``added_epoch`` /
# ``revoked_epoch`` ordinal cursor. No new event *body* type (``KEY_ADD``/``KEY_REVOKE`` are
# unchanged on the wire), so the unknown-type forward-compat path is not engaged; the bump
# records the projection change so clones agree on the keyring shape.
SCHEMA_VERSION = 5

# Types that appear on disk but are intentionally NOT in KNOWN_EVENT_TYPES because
# they are handled OUTSIDE the main replay dispatch: the bridge-only ``SYNC`` and
# the externally-scanned ``PRECONDITIONS``. They are recognized by this binary, so
# the forward-compat "newer than me" warning must NOT flag them.
_NON_REPLAY_KNOWN_TYPES = frozenset(
    {
        "SYNC",
        "PRECONDITIONS",
        "REVIEW_RESULT",
        "COMPLETION_VERDICT",
        "TICKET_DIGEST",
        "ENQUEUE_ENRICH",
        "CLAIM_ENRICH",
        "DONE_ENRICH",
    }
)


def is_unknown_newer_type(event_type: str) -> bool:
    """True when ``event_type`` was written by a NEWER rebar this binary does not
    understand — i.e. neither in the replay dispatch set (``KNOWN_EVENT_TYPES``)
    nor a recognized non-replay type (``SYNC``/``PRECONDITIONS``). Used by ``fsck``
    / ``bridge_fsck`` to surface the otherwise-silent forward-compat window."""
    return bool(event_type) and (
        event_type not in KNOWN_EVENT_TYPES and event_type not in _NON_REPLAY_KNOWN_TYPES
    )


# The TAG_DELTA event type name — a single source of truth shared by the reducer
# dispatch (here, via KNOWN_EVENT_TYPES), the write-path allow-list
# (``_store.event_append.EVENT_TYPES``), and every emitter (leaf/composer/inbound),
# so the literal cannot drift between them.
TAG_DELTA = "TAG_DELTA"

# The KEY_ADD / KEY_REVOKE event type names (epic gnu-whale-ichor / e165) — a single
# source of truth shared by the reducer dispatch (here, via KNOWN_EVENT_TYPES), the
# write-path allow-list (``_store.event_append.EVENT_TYPES``), and the identity write
# gate, so the literals cannot drift between them.
KEY_ADD = "KEY_ADD"
KEY_REVOKE = "KEY_REVOKE"

# Every event_type the reducer's processor dispatch (_processors.replay) applies.
# Anything outside this set is forward-compat payload: preserved-and-ignored.
KNOWN_EVENT_TYPES = frozenset(
    {
        "CREATE",
        "STATUS",
        "COMMENT",
        "LINK",
        "UNLINK",
        "BRIDGE_ALERT",
        "REVERT",
        "EDIT",
        "FILE_IMPACT",
        "VERIFY_COMMANDS",
        "SIGNATURE",
        "ARCHIVED",
        "SNAPSHOT",
        # Workflow run-state (epic a88f / WS-C1). A workflow run and its per-step
        # records persist as events on the TARGET ticket; the reducer folds them
        # into ticket state as the lazy per-key maps workflow_runs / workflow_steps.
        # Known (not forward-compat) so compaction squashes them into a SNAPSHOT
        # (their effect is preserved in compiled_state, restored by process_snapshot).
        "WORKFLOW_RUN",
        "WORKFLOW_STEP",
        # Commits-on-ticket (epic a88f / WS-H): commit SHAs attached to a ticket as
        # a durable, union-merged list. NOT a Jira-synced field (the outbound differ
        # is field-driven and never reads it).
        "COMMITS",
        # Tag add/remove deltas (epic P2.3): replace whole-field EDIT.tags (LWW
        # clobber) so concurrent tag edits converge. Folded by process_tag_delta.
        TAG_DELTA,
        # Identity key lifecycle (epic gnu-whale-ichor / e165): signed add/revoke folded
        # into an epoch-scoped keyring by process_key_event. Known (not forward-compat)
        # so compaction squashes them into a SNAPSHOT (the keyring/keyring_epoch are
        # preserved in compiled_state, restored by process_snapshot).
        KEY_ADD,
        KEY_REVOKE,
    }
)

# ── Creation-channel vocabulary (epic jira-reb-977, story 6fe2) ─────────────────
# The closed set of public ingresses that can stamp a genesis CREATE event with a
# `creation_channel`. This runtime constant MUST stay in lockstep with the schema
# `creation_channel` enum `$def` in `schemas/common.schema.json`; a contract test
# (`tests/interfaces/contracts/test_creation_channel_vocabulary.py`) pins the two
# together so the vocabulary cannot drift between the wire schema and the validator.
#   * cli / mcp / python — the three LOCAL public interfaces (this story).
#   * jira / import       — reserved for later stories (Jira-inbound / NDJSON import).
#   * unknown             — a PROJECTION-ONLY fallback for a legacy CREATE that carried
#                           no channel; it is NEVER a valid live-write value.
CREATION_CHANNELS = frozenset({"cli", "mcp", "python", "jira", "import", "unknown"})


def validate_creation_channel(value: str) -> str:
    """Return ``value`` iff it is a valid LIVE-WRITE creation channel, else raise.

    A live write must name a real ingress from :data:`CREATION_CHANNELS`, so any value
    outside that set is rejected. ``"unknown"`` is ALSO rejected here even though it is
    a member of the vocabulary: it is a projection-only fallback the reducer applies to
    a legacy CREATE that carried no channel, never a value a writer may stamp. Raises
    :class:`ValueError` on any violation."""
    if value == "unknown":
        raise ValueError(
            "creation_channel 'unknown' is a projection-only fallback and cannot be "
            "written at genesis"
        )
    if value not in CREATION_CHANNELS:
        raise ValueError(
            f"invalid creation_channel {value!r}; must be one of "
            f"{sorted(CREATION_CHANNELS - {'unknown'})}"
        )
    return value
