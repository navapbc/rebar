# ADR 0097 — Many-to-many tracker projects

**Status:** Accepted
**Date:** 2026-08-14
**Supersedes:** N/A
**Superseded by:** N/A
**Relates to:** [ADR 0060](0060-bridge-state-format-changes.md) — this mapping
satisfies its cheap/low-churn bridge-state constraint by construction (see Decision 1)
**Ticket:** `7032-d9f3-ed89-4f7b`

## Context

Before this epic a store synced exactly one tracker project: `jira.project` (env
`JIRA_PROJECT`) was a single scalar, the inbound fetcher issued one JQL pair against
that one key, and every inbound-created ticket recorded no source project at all. A
store that needed to bridge several Jira projects — the common case once one rebar
store backs several repositories — could not.

The epic makes the project set a first-class, many-to-many property. A committed
`.bridge_state/projects.json` mapping records `{key: {"repos": [...]}}` for every
project the store syncs plus a `legacy_default`; tickets carry a tri-state
`bridge_project` field and a `repos` field (story cef7); the inbound fetcher fans out
per project (story 1734); and the outbound differ routes each ticket to its
`bridge_project`. This ADR records the three load-bearing decisions that shape that
design, because each was chosen against a plausible alternative a future reader would
otherwise be tempted back toward.

## Decision 1 — the project set belongs to the STORE, not to a repo

The sync list lives in one committed file per store (`.bridge_state/projects.json` on
the tickets branch), keyed by project. It is a property of the store as a whole.

**Chosen over:** a per-repo project list (each repo declaring, in its own checkout,
which projects it bridges).

**Why:** the reconciler reconciles the whole store against one shared `prev_snapshot`
per pass — the snapshot is the store's single view of the remote, and every project's
inbound records merge into that one snapshot (deduped by jira key) before the reducer
applies them. A per-repo project list has no coherent place to attach that shared
snapshot: two repos claiming overlapping or disjoint project sets would each need a
private view of the remote, fragmenting the one snapshot the reconciler is built
around and making "did this pass see every project" unanswerable. Store ownership
keeps exactly one authority for the sync list and one snapshot reconciled against it.

**Relationship to [ADR 0060](0060-bridge-state-format-changes.md).** ADR
0060 requires bridge state to stay committed but cheap — low-churn formats that do not
rewrite on every pass. The mapping satisfies that constraint *by construction*: it is
a single small JSON object keyed by project, written only when an operator changes the
sync list (`rebar bridge projects set/remove`) or when the one-time seed runs — never
per ticket and never per pass.

## Decision 2 — the legacy migration writes NO ticket events

Migrating a store initialized before this feature is done by two ensure-registry units
(`projects-seed`, `projects-compat-stamp`) that stamp two committed tickets-branch
files and emit **zero** ticket events. Legacy tickets — those with no `bridge_project`
field — are never rewritten; they resolve to the mapping's `legacy_default` at read
time via the tri-state `None` sentinel.

**Chosen over:** an EDIT-per-ticket backfill that stamps an explicit `bridge_project`
onto every pre-epic ticket.

**Why:** the ensure units run under the store write lock inside `run_ensures` (at
`init`, `fsck --repair`, and MCP boot). A backfill would emit one EDIT event per legacy
ticket — O(tickets) events — under that single lock, blowing the short lock budget the
ensure sweep is designed to hold and making a routine `init` on a large legacy store
pause for the whole rewrite. The tri-state field makes the backfill unnecessary: an
absent `bridge_project` deliberately means "resolve to `legacy_default`", so the seed
only has to record the default once, not stamp every ticket. The migration is
therefore a two-file stamp that is idempotent and cheap to re-run.

## Decision 3 — the compatibility capability is stamped CONDITIONALLY

The `multi-project-bridge` capability is written into `.store-compat.json`'s
`required_capabilities` only once the mapping actually holds more than one project. The
`projects-compat-stamp` unit is level-triggered on the mapping's *state* — it stamps
when the store is genuinely multi-project, regardless of how it got there.

**Chosen over:** stamping the capability unconditionally the moment the feature ships
(so every store on a new binary declares it).

**Why:** the store-compat record is a fail-closed forward guard — an older binary that
does not list a store's `required_capabilities` entry refuses the store rather than
corrupting it. Stamping unconditionally would make every single-project store — the
overwhelming majority, and ones that use none of this feature — suddenly unreadable by
older binaries in a mixed-version fleet, for no benefit. Conditional stamping demands
the capability of exactly the stores that depend on it: a single-project store stays
backward-compatible, and only a genuinely multi-project store fails closed against a
binary too old to reconcile it correctly. This is the expand half of an
expand/contract rollout — `multi-project-bridge` is registered in
`KNOWN_CAPABILITIES` first, so current binaries pass, before any store declares it.

## Consequences

- One store can bridge any number of tracker projects; `rebar bridge projects
  {list,set,remove}` manages the mapping.
- Legacy stores upgrade transparently at the next `init`/`fsck --repair`/MCP boot with
  no event-log churn; a single-project store stays readable by older binaries.
- A multi-project store fails closed against a binary that predates the
  `multi-project-bridge` capability — the operator's remedy is to upgrade that binary
  (see [jira-sync-setup.md](../jira-sync-setup.md) and
  [user-guide.md](../user-guide.md)).
- The mapping is the single authority for the sync list; a ticket's `bridge_project`
  is resolved against it (or the `legacy_default`) rather than validated against it, so
  an unknown key is a routing target, not a hard error.
