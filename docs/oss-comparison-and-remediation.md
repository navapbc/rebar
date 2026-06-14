# rebar vs. OSS ticket systems — gaps, gotchas, and a remediation strategy

*Analysis date: 2026-06-13. Compares rebar against popular open-source ticket /
issue trackers, identifies functional gaps, surfaces gotchas those projects
handle that rebar doesn't, flags best practices rebar doesn't follow, and
proposes a value-vs-risk-prioritized remediation roadmap.*

## 1. Framing: what rebar is, and a fair comparison set

rebar is not a general-purpose tracker; it is a **git-native, event-sourced
coordination store for parallel coding agents** (CLI + Python library + MCP),
with a Jira reconciler for humans-in-the-loop. A fair comparison weighs it
against systems with the same DNA — *distributed, git-backed, no-daemon
trackers* — and borrows "best-practice" lessons from heavier trackers only where
they transfer.

| System | Storage | Interfaces | Concurrency story | Agent affordances |
|--------|---------|-----------|-------------------|-------------------|
| **rebar** | git orphan branch, append-only JSON events, replay | CLI, lib, MCP | **Strong** — atomic `claim`, optimistic concurrency, union-merge, deterministic STATUS forks | **Best-in-class** — `next-batch`, file-impact, quality gates, scratch |
| **git-bug** | git objects (ops), Lamport-clock ordered | CLI, TUI, **web UI**, **GraphQL** | Strong — operation CRDTs, **Lamport clocks**, identities | None |
| **git-issue** (dspinellis) | text files in git | CLI | Basic — git merge, no claim | None |
| **Fossil tickets** | SQLite, sync protocol | **web UI**, CLI | Field-level merge, **SQL report queries** | None |
| **dstask / taskwarrior** | git (dstask) / proprietary | CLI | Last-write / git merge | None (personal TODO) |

**Where rebar already leads** (don't regress these copying others): the atomic
`claim` primitive, conflict-aware `next-batch` scheduling via `file_impact`, the
per-ticket quality gates, three-interface parity pinned by JSON-Schema +
golden tests, deterministic UUID-keyed STATUS-fork resolution, and
preserve-and-ignore forward compatibility for unknown event types. No other
git-backed tracker surveyed has a real multi-writer *claim* story or agent-aware
scheduling. The gaps below are about the long tail, not the core.

---

## 2. Gotchas other projects handle that rebar doesn't

These are correctness/operational traps, not missing features — the higher-value
half of this report.

### G1. Cross-clone ordering uses wall-clock time, not a logical clock
rebar orders replay by the `${timestamp_ns}` filename prefix and explicitly
documents (invariant I8) that **COMMENT/EDIT interleaving across clients is
"best-effort" under clock skew** — only STATUS forks are skew-independent (UUID
keyed). Consequence: two agents on two clones with skewed clocks editing the
same `description`/`priority`/`assignee` resolve by *last wall-clock writer*, so
a causally-earlier edit on a fast clock can silently clobber a causally-later
edit on a slow clock. Comment threads can also reorder.

**git-bug solves exactly this with Lamport logical clocks** (`MemClock` +
`PersistedClock`), giving causal, skew-immune ordering of creation/edit
operations. rebar acknowledges the weakness but stops at STATUS forks; EDIT and
COMMENT are left skew-sensitive **and untested for convergence** (the regression
suite pins STATUS-fork convergence, not EDIT/COMMENT). This is the single
highest-value correctness gap.

### G2. No authenticated identity; author/assignee are free strings
`COMMENT.author`, `CREATE.assignee`, and `claim --assignee` are unauthenticated
strings. Anyone with push access to `origin/tickets` can forge any author or
re-attribute a claim, and replay trusts the bytes. **git-bug has first-class
identities** and rides git's commit-signing for authenticity. For a store whose
*entire value proposition is multi-writer coordination and an audit trail*, the
"who actually did this" question has no trustworthy answer today.

### G3. Unbounded git object growth; gc is disabled by design
Every mutation is a new committed file, and the sync algorithm sets
`gc.auto=0` so a stray `git gc` can't reclaim the reflog commits it relies on
for the reset-recovery safety net. The trade is **unbounded loose-object / pack
growth** on long-lived `tickets` branches. Compaction exists but is per-ticket
and operator-driven; there is no safe, branch-wide `gc`/repack story. git-bug
packs ops into fewer objects with a documented cache; Fossil's SQLite sidesteps
small-object blowup entirely. rebar's README *sells* "built to scale," but the
maintenance path to keep it scaling isn't shipped.

### G4. Collection-field edits replace instead of merge
`edit_ticket(tags=[...])` writes an EDIT that sets the whole `tags` field
(last-writer-by-replay-order). Combined with G1's skew, two agents concurrently
adding different tags can lose one. The dedicated `tag`/`untag` events are
delta-shaped and safe, but the `edit(tags=)` path and any future collection
field (labels, watchers) inherit replace-semantics. git-issue/git-bug model
label changes as add/remove operations that merge as a set.

### G5. Search/query is substring-AND only
`search` is whitespace-split, case-insensitive **AND of substrings** plus a few
fixed flags; no OR, no field-scoped predicates (`assignee:`, `priority:<2`), no
caller-controlled `sort`, no saved queries. Fossil exposes SQL report queries;
git-bug has a `status:open label:x sort:…` query language. Agents currently
post-filter in their own code, which fragments behavior across callers.

---

## 3. Functional gaps (feature parity)

Confirmed absent from rebar's native model (verified against the schema, the
command surface, and the event types — several appear only as *Jira-side* fields
inside the reconciler, not as native rebar fields):

| Capability | rebar | Comparators that have it | Relevance to rebar's mission |
|-----------|-------|--------------------------|------------------------------|
| **Human read UI** (web/TUI) | none | git-bug (web+TUI), Fossil (web) | **High** — rebar's stated audience includes humans-in-the-loop, who today see tickets only via Jira |
| **GitHub/GitLab bridge** | Jira only | git-bug, git-issue | **High** for OSS adoption |
| **Export / import** | git history only | git-issue (GH/GL import+export) | **Medium-high** — migration & backup |
| **Due dates / milestones** | none (epics ≈ grouping) | git-issue, Fossil | Low for agents; matters for Jira parity |
| **Time tracking / estimates / points** | none | git-issue, most trackers | Low for agents |
| **Watchers / notifications / webhooks** | none | git-issue (watchers) | **Medium** — humans can't be pinged when an agent touches their ticket |
| **Attachments** | none | git-bug, git-issue | Low-medium for agents |
| **Labels with color/description** | bare string tags | most trackers | Low |
| **Comment threading / reactions / edit** | flat append only | git-bug, Fossil | Low |
| **Custom fields / templates** | fixed schema | Redmine, Fossil | Low (gates enforce a template instead) |

Native model for reference — types: `task|story|bug|epic`; statuses:
`open|in_progress|blocked|closed|archived|deleted`; priority `0–4`; six
relations; fields `title, description, priority, assignee, parent_id, tags,
comments, deps, file_impact`. Full event history *is* the audit log (a genuine
strength), so "no audit log" is **not** a gap.

---

## 4. Best practices rebar doesn't (fully) follow

- **No logical clock** (G1) — the canonical best practice for distributed
  ordering; rebar uses wall-clock ns.
- **No identity/signing** (G2) — trust boundary on a shared, pushable branch.
- **No maintenance/gc story** (G3) — disabling gc without shipping a safe
  reclaim path is an operational footgun.
- **Untested convergence for the skew-sensitive paths** — STATUS forks are
  pinned by `test_concurrency_regression.py`; EDIT/COMMENT convergence under
  skew is documented as "best-effort" and left unverified, so the known weak
  spot has no executable guard.
- **Mixed bash/Python engine** — the project already tracks this (strangler-fig
  migration in `docs/bash-migration.md`); it's a real maintainability tax and a
  portability constraint (must install unpacked, needs `bash`/`jq`/`flock`).
- **Self-hostable read surface** — every comparator gives a human a way to *look*
  without a second system; rebar outsources that to Jira.

Practices rebar **does** follow well, worth noting so they're preserved:
schema-versioned wire format with forward-compat, JSON-Schema-validated outputs
across all three interfaces, golden/parity tests, documented invariants as a
merge gate, and Apache-2.0 licensing with release notes.

---

## 5. Remediation strategy (prioritized by value × risk)

Sequenced to front-load **high user value at low risk** and to respect the I1–I9
invariants (every item below is designed to *not* require a new cross-client
lock or a committed shared-mutable index).

```
 high value │  G5 query++      │  G1 logical clock
            │  export/import   │  identity+signing (G2)
            │  gc/maintenance  │  GH/GitLab bridge
            │  (G4 tag-merge)  │  read-only web/TUI viewer
   value    ├──────────────────┼─────────────────────────
            │  labels meta     │  attachments
  low value │  comment edit    │  due dates / time tracking
            └──────────────────┴─────────────────────────
                 low risk              higher risk
```

### Phase 1 — Quick wins (high value, low risk; additive, no wire change)
1. **Query upgrade (G5).** Add field-scoped predicates (`assignee:`,
   `priority:<2`, `type:`, `tag:`), `OR`, and a `--sort` flag to `search`/`list`,
   reusing the existing in-memory reducer filter path. Purely read-side (I3
   clean), no schema change, immediately useful to every agent. *Test:* extend
   reducer search/filter unit tests + a golden.
2. **`rebar export` / `rebar import` (G-export).** `export` = stable JSON dump of
   replayed state (the output schemas already exist); `import` = create events
   from a JSON/GitHub-issues payload through the normal locked write path. Fully
   additive, enables migration/backup/CI snapshots.
3. **Collection-field merge fix (G4).** Make `edit_ticket(tags=)` compose
   `tag`/`untag` *deltas* against current state instead of writing a
   whole-field EDIT (or model `tags` as a 2P-set in the reducer). Removes a
   silent-loss path with no format break. *Test:* concurrent add-different-tags
   convergence case.
4. **`rebar gc` + maintenance doctrine (G3).** A guarded command that repacks the
   `tickets` branch and prunes only beyond the reflog-safety window the sync
   algorithm depends on, plus a documented "compact + gc" cadence. Closes the
   unbounded-growth footgun without touching the recovery invariants. *Test:*
   gc-then-recover regression (ensure reset-recovery still works post-gc).

### Phase 2 — Correctness backbone (highest value, medium risk; phase carefully)
5. **Hybrid Logical Clock for event ordering (G1).** Replace the raw
   `${timestamp_ns}` filename prefix with a fixed-width, lexically-sortable **HLC**
   (max(physical, seen+1)), persisted per-clone under `.rebar/` (local,
   gitignored, rebuildable from the max prefix seen — like git-bug's
   `PersistedClock`). Backward-compatible: the prefix stays a sortable string, so
   older clones still replay by lexical order; bump `SCHEMA_VERSION`. This makes
   EDIT/COMMENT ordering causal and skew-immune, generalizing I8 beyond STATUS
   forks. *Risk:* it changes the ordering key — gate it behind the existing
   forward-compat machinery and **add the missing EDIT/COMMENT convergence
   regression test** as part of the change.
6. **Authenticated identity + optional signing (G2).** Record a resolved author
   identity (from git config, optionally GPG/SSH-signed) on write events and
   surface verified/unverified on replay; keep it **opt-in** so the
   zero-config path is unchanged. Pairs naturally with HLC (both are
   per-actor). *Risk:* key management — ship verification as advisory first
   (warn, don't reject) to avoid wedging existing stores.

### Phase 3 — Reach & humans-in-the-loop (high value, higher effort)
7. **GitHub/GitLab bridge.** Reuse the reconciler's differ/applier architecture
   (already abstracted for Jira) to add a GitHub Issues bridge — the biggest
   single lever for OSS adoption.
8. **Read-only web/TUI viewer.** A thin, *read-only* server/TUI over the existing
   replayed JSON (no new write path, I-invariant-neutral) so humans can browse
   the tracker without Jira. Start read-only to keep risk low.
9. **Notifications hook.** A post-write hook (or `rebar watch`) that emits to a
   file/webhook so humans/CI can react to ticket changes; no committed shared
   state (I6 clean).

### Phase 4 — Long tail (lower value for the agent mission; do only on demand)
Attachments (git-blob-referenced, mind G3 bloat), due dates / milestones / time
tracking (mainly for tighter Jira parity), label metadata, comment editing. Defer
unless a concrete user need surfaces — these add surface area against rebar's
deliberately small model.

### Recommended cut line
Phases 1–2 are the high-leverage core: Phase 1 is days of low-risk additive work
that materially improves daily agent UX and operational safety; Phase 2 closes
the one architectural correctness gap (skew-sensitive ordering) that a
distributed tracker should not ship without, and brings rebar to parity with
git-bug's clock model while keeping its unique agent-coordination strengths.

---

## Sources

- [git-bug — decentralized issue tracker](https://github.com/git-bug/git-bug) and its [Lamport clock package](https://pkg.go.dev/github.com/MichaelMure/git-bug/util/lamport)
- [git-issue (dspinellis) — git-based decentralized issue management](https://github.com/dspinellis/git-issue)
- [dstask — git-powered taskwarrior alternative](https://github.com/naggie/dstask)
- [Fossil — bug-tracking theory](https://fossil-scm.org/home/doc/tip/www/bugtheory.wiki) and [web UI](https://fossil-scm.org/draft/doc/0d7ac90d575004c2415/www/webui.wiki)
- [The trouble with timestamps (Kyle Kingsbury)](https://aphyr.com/posts/299-the-trouble-with-timestamps) — why wall-clock ordering is unsafe in distributed systems
- rebar internal docs: `docs/concurrency.md` (I1–I9), `docs/event-schema.md`, `docs/architecture.md`
