"""Ticket state helpers: initial state factory, error-state builder, and the
shared terminal-status predicate."""

from __future__ import annotations

# --- terminal-state predicate (bug e63c) ---
# THE definition of "this blocker no longer blocks", shared by every reader:
# `graph/_ready.py`, `graph/_unblock.py`, `graph/_graph.py` and
# `_engine_support/next_batch.py`. Those four once carried three different
# memberships, so `next-batch` offered a ticket that `ready` withheld.
#
# It lives here rather than in `rebar.types` because that module is GENERATED from
# the canonical JSON Schemas (`python -m rebar.schemas.gen_types`, CI-enforced), and
# a predicate is behaviour, not a schema-derived type. All four readers already
# depend on `rebar.reducer`, so this home adds no new dependency edge -- which is
# what keeps `_ready.py` dependency-light as its docstring promises.
#
# Each member is a terminal state a ticket cannot leave on its own; every member
# must be a real TicketStatus (pinned by a test against the schema's status enum).
TERMINAL_STATUSES: frozenset[str] = frozenset({"closed", "archived", "deleted"})


def is_terminal_status(status: str) -> bool:
    """True when ``status`` is terminal, i.e. a blocker in it no longer blocks."""
    return status in TERMINAL_STATUSES


def make_initial_state() -> dict:
    """Return a fresh empty ticket state dict with all standard schema fields."""
    return {
        "ticket_id": None,
        "ticket_type": None,
        "title": None,
        "status": "open",
        # Derived lifecycle posture for plan reviews. STATUS and SNAPSHOT folding own updates.
        "plan_review_phase": "planning",
        "author": None,
        "created_at": None,
        "env_id": None,
        "parent_id": None,
        "priority": None,
        "assignee": None,
        # Claiming coding-agent session provenance, folded on the open->in_progress edge
        # (epic crust-fetch-stump; claimed_session = story 68ef, claim_harness /
        # claim_remote_session = story c557). Defaulted here so a pre-feature snapshot / event
        # replays to an explicit None (key-present) rather than key-absent.
        "claimed_session": None,
        "claim_harness": None,
        "claim_remote_session": None,
        "alias": None,
        "description": "",
        "tags": [],
        # Bridge/project fields (story cef7). `bridge_project` is TRI-STATE: None is the
        # deliberate "absent/legacy" sentinel (mirrors file_impact_scope's seeded
        # sentinel), "" means explicit never-sync, and a non-empty key is a sync target.
        # Seeded None so a no-flag create leaves it None; the CREATE processor overwrites
        # it ONLY when the event data actually carries the key (present-only projection).
        "bridge_project": None,
        "repos": [],
        "comments": [],
        "deps": [],
        # Managed-reference provenance (story safe-luge-nog): a strictly-monotonic,
        # compaction-surviving projection of every logical reference this ticket has
        # ever managed (parent / link relations), as sorted [kind, target] pairs.
        # Drives the shared removal-sync gate (see reducer._managed_refs).
        "managed_refs": [],
        "bridge_alerts": [],
        "reverts": [],
        "file_impact": [],
        "file_impact_scope": "undeclared",
        "no_file_impact_reason": "",
        "verify_commands": [],
        "signature": None,
        # Identity key lifecycle (epic gnu-whale-ichor): a POSITION-based keyring of
        # {public_key, added_at, revoked_at} records, where a position is an event's
        # `{timestamp}-{uuid}` filename prefix (the git-commit-ancestry validity model,
        # SCHEMA_VERSION 5 — no epoch cursor). Seeded empty here so a non-identity ticket
        # replays to an explicit empty keyring rather than key-absent, and an identity's
        # genesis / KEY events fold onto this base.
        "keyring": [],
        # Authorship PRESENCE summary (epic gnu-whale-ichor / 3183): a per-ticket count of
        # folded events that DID vs DID NOT carry an `author_sig` on their envelope. This is
        # PRESENCE ONLY — never a cryptographic check (that is the merge-gate `verify-authorship`
        # + the commit-ancestry verify). Seeded here so a pre-feature snapshot / event replays
        # to an explicit zeroed summary rather than key-absent; the replay loop increments it.
        "authorship": {"signed": 0, "unsigned": 0},
        "preconditions_summary": {"status": "pre-manifest"},
        "parent_status_uuid": "",
    }


def make_error_dict(ticket_id: str, status: str, error: str) -> dict:
    """Build an error-state dict with all standard schema fields (d145-e1a9).

    Ensures consumers iterating ticket_type/title never crash on missing keys,
    regardless of which error path produced the dict. Built from
    :func:`make_initial_state` so the two share ONE field list (previously two
    near-identical literals — the error dict had drifted to OMIT
    ``preconditions_summary``; basing it on the canonical schema fixes that and
    guarantees the key sets stay in lock-step).
    """
    state = make_initial_state()
    state.update(
        {
            "ticket_id": ticket_id,
            "title": f"[{status}] {error} for {ticket_id}",
            "status": status,
            "error": error,
        }
    )
    return state
