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
"""

from __future__ import annotations

# Bump when the event wire format changes in a way other clones must be aware of.
# v2 (P2.1): the ``${timestamp}`` filename-prefix is now a single-integer Hybrid
# Logical Clock value (``max(cache, witnessed max-prefix, time_ns()) + 1``) rather
# than a raw ``time.time_ns()``. Same width (19 digits until ~2286) and same
# single-integer encoding, so older clones still string-compare correctly; the only
# *semantic* change is skew-immune causal ordering. No event-body change, so the
# unknown-type forward-compat machinery is not engaged.
SCHEMA_VERSION = 2

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
    }
)
