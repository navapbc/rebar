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
# v3 (P2.3): the new ``TAG_DELTA`` event body carries add/remove tag deltas so
# concurrent tag edits converge (no whole-field clobber). This is the FIRST bump
# that adds a new event *body* type, so it DOES engage the unknown-type forward-
# compat path: an older (v2) clone preserves-and-ignores ``TAG_DELTA`` (the file
# survives, the mutation is invisible until it upgrades). The integer itself is
# declarative only — forward-compat is governed by ``KNOWN_EVENT_TYPES`` below, not
# by this value; nothing gates behavior on it.
SCHEMA_VERSION = 3

# Types that appear on disk but are intentionally NOT in KNOWN_EVENT_TYPES because
# they are handled OUTSIDE the main replay dispatch: the bridge-only ``SYNC`` and
# the externally-scanned ``PRECONDITIONS``. They are recognized by this binary, so
# the forward-compat "newer than me" warning must NOT flag them.
_NON_REPLAY_KNOWN_TYPES = frozenset({"SYNC", "PRECONDITIONS", "REVIEW_RESULT", "TICKET_DIGEST"})


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
    }
)
