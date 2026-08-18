# The rebar ticket model

This is the agent-facing guide to rebar's **ticket model** — the four concepts you
reach for constantly when driving work through the store: the `idea` status, the
parent/child **hierarchy**, **links** between tickets, and **tags**. It documents the
*concepts and the surface you drive them through*; the underlying append-only **event
mechanics** (the `CREATE` / `STATUS` / `LINK` / `UNLINK` / `TAG_DELTA` event bodies and
how they replay) live in [event-schema.md](event-schema.md), and this page
cross-references that document rather than restating event bodies, so the two never
drift.

A ticket is one of five types — `task`, `story`, `bug`, `epic`, or `session_log` — and
carries a **status**, an optional **parent**, a set of **links** to other tickets, and a
set of **tags**. Work statuses are `open`, `in_progress`, `blocked`, and `closed`; the
`idea` status below is a fifth, pre-work status. (`session_log` is a gate/lifecycle-exempt
type documented in [event-schema.md](event-schema.md) under "The session_log ticket type"
and in [user-guide.md](user-guide.md), not here.)

## The `idea` status — a parking lot for undesigned work

`idea` is a first-class ticket **status** (any ticket type can hold it) for future work
that is **captured but not yet designed or ready to implement** — a durable parking lot,
distinct from `open` (which means "designed enough to work; eligible for
`ready`/`next-batch`"). It exists because the only other pre-work status is `open`, and an
`open` ticket is immediately claimable work; `idea` gives you a place to record a rough
idea without it becoming dispatchable. It is a status rather than a tag deliberately:
`claim` only accepts `open` tickets, so an `idea` ticket is **structurally unclaimable**,
with no genesis window in which it is momentarily `open`.

- **Transitions are free.** rebar does not enforce a rigid state machine — you can
  `transition <id> open idea`, `idea open`, `idea in_progress`, etc. (`idea` is a valid
  `current`/`target` status everywhere `transition` is used).
- **Excluded from dispatch (by omission).** `idea` tickets **never** appear in `ready` or
  `next-batch` — those surfaces only consider `open`/`in_progress`, so an undesigned idea
  is never scheduled as parallel work.
- **Fully listable/searchable.** `list --status=idea` returns them and `search` matches
  them, so ideas can always be found and later promoted (`idea → open`).
- **`idea → closed` skips the completion gates.** Rejecting/dropping an idea closes with
  **no** completion-verifier / signature / bug-close-class gate (an undesigned idea has
  nothing to verify) — but the **structural open-children guard still holds** (you cannot
  close a parent that has open children).
- **Exempt from noisy `validate` findings.** `idea` tickets do not contribute
  empty-epic / orphan / missing-description / interface-contract / count findings to the
  store-health score (an idea is *expected* to be loosely specified); genuine structural
  checks (e.g. cycles) still apply.
- **Jira: `idea ↔ IDEA`.** `idea` round-trips to the Jira status `IDEA` through the
  reconciler, subject to the usual workflow-transition prerequisite (the target Jira
  workflow must permit the transition into `IDEA`) — see
  [jira-sync-setup.md](jira-sync-setup.md) "The `idea` status ↔ Jira `IDEA`" for the
  operator prerequisite, deployment sequencing, and the convergence quirk.
- **Capture in one atomic step.** `rebar idea "<title>"` (and the MCP `create_idea` /
  library `rebar.idea(...)`) creates a ticket **directly** in status `idea` in a single
  genesis event — never momentarily `open`/claimable. This is the one command that emits a
  non-`open` genesis `status` on the `CREATE` event (see
  [event-schema.md](event-schema.md), the `CREATE` row).

## Closing a bug — the required `--class` classification

Closing a **bug** (from any non-`idea` status) requires a bounded `--class <value>` that
records **why** it closed. The value is folded onto the `*->closed` edge into reduced state,
so `rebar show <bug> --output json` surfaces `close_class`. The closed vocabulary is exactly:

| class | meaning |
| --- | --- |
| `regression` | a change reintroduced a previously-fixed defect |
| `plan_defect` | the design/plan itself was wrong |
| `env_integration` | an environment or integration issue |
| `flaky` | a nondeterministic/intermittent failure |
| `preexisting` | a latent defect that predated the work |
| `not_a_bug` | behaved as intended |
| `duplicate` | already tracked elsewhere |
| `escalated` | handed off to the user/another owner |
| `obsolete` | the premise no longer holds |
| `superseded` | replaced by a newer ticket |
| `wontfix` | a deliberate decision not to do the work |
| `undetermined` | the escape hatch when no class fits |

A missing or out-of-vocabulary `--class` is refused (the error names the allowed values).
`--class` **replaces** the former free-text `--reason` requirement for bug closes.
(`idea → closed` is a reject/drop and skips this gate.)

When the completion-verification close gate is enabled, `duplicate`, `not_a_bug`, and
`escalated` describe non-completion dispositions and skip completion verification only
when their evidence holds. For `duplicate` that evidence is a net-active
`bug -duplicates-> canonical` link to a live ticket, or a live replacement with a
net-active `replacement -supersedes-> bug` link. `not_a_bug` and `escalated` are
**reason-required** (bug d54b): a live replacement link (same shapes as above) satisfies
them and is checked first, but without one the close REQUIRES `--reason=<text>` — why no
defect exists, or where the work was escalated to — which persists as `close_reason` and
is signed into the disposition attestation. A close with neither is refused at write time
naming both doors; it never falls through to the completion verifier (which would demand
proof that a nonexistent defect was fixed). A missing, reversed, unlinked, unresolved,
archived, or deleted replacement does not count as a replacement.

## Administrative close dispositions (any ticket type)

A non-bug ticket (task/story/epic/…) normally closes without `--class`, and its close is
what the completion-verification gate scores. When the work is **not being completed** —
the ticket is a duplicate, was superseded, its premise evaporated, or it is a deliberate
wontfix — closing it "as done" would be dishonest and the verifier would rightly refuse.
The sanctioned door is the **administrative subset** of the same `--class` vocabulary:

| class | evidence demanded |
| --- | --- |
| `duplicate` | a net-active `ticket -duplicates-> canonical` link to a live ticket |
| `superseded` | a live replacement with a net-active `replacement -supersedes-> ticket` link |
| `obsolete` | a free-text justification: `--reason=<why the premise no longer holds>` |
| `wontfix` | a free-text justification: `--reason=<why the work is declined>` |

```sh
rebar transition <id> in_progress closed --class=obsolete --reason="premise removed by epic X"
rebar link <dupe> <canonical> duplicates && rebar transition <dupe> in_progress closed --class=duplicate
```

Any other class on a non-bug close is refused (the error names the four allowed values);
`obsolete`/`wontfix` without `--reason` are refused. The reason is folded onto the close
edge as `close_reason` in reduced state (distinct from `force_close_reason`), and the
completion-verifier close gate mints a **disposition verdict** from the link or reason
instead of running LLM completion verification — the signed attestation manifest records
`disposition: <class>` plus the replacement id or reason, so the bypass is auditable, not
silent. Bug closes may also use `obsolete`/`superseded`/`wontfix` under the same evidence
rules. A forced close (`--force=<reason>`) remains the unaudited escape hatch; its refusal
now hints at `--class` when an administrative disposition would fit.

## Hierarchy and containment (`parent_id`, not a link)

Containment (epic → story → task/bug) is the **`parent_id`** hierarchy, **not** a `link`
relation. Parent a ticket to the epic/story it belongs to with `create --parent <id>` or
`edit --parent <id>`. **Do not** attach an epic's workstreams with a `depends_on` /
`discovered_from` link — **parent** them, or they aren't its children. The hierarchy is
what `ready` / `next-batch` / `validate` and the completion gate's child-closure check all
operate on; a link cannot substitute for it.

Two consequences of the hierarchy that you will hit in practice:

- **Parent-first claim/transition cascade.** Starting work on a child pulls its still-`open`
  parent into progress first — claiming a leaf task moves its open story and open epic to
  `in_progress` too, carrying the same assignee up the chain. Only the `open → in_progress`
  direction cascades; `close`/`reopen`/`blocked` never do. When the plan-review claim gate
  is enabled the cascaded parent claim runs the **parent's own** gate, so a leaf claim can
  be blocked by a parent's missing/stale attestation. The full contract — the up-the-chain
  recursion, the fail-fast semantics, the cross-agent race ownership policy, and the gate
  interaction — is documented in [concurrency.md](concurrency.md) under "Parent-first
  claim/transition cascade".
- **The open-children guard on close.** A parent cannot be closed while it has open
  children (this holds even for `idea → closed`). Close subtrees bottom-up.

## Linking (the seven relations + hierarchy escalation)

`link <id1> <id2> <relation>` **requires** a relation. There are seven:

| Relation | Meaning | Directional? | Can create a cycle? |
|----------|---------|--------------|---------------------|
| `blocks` | id1 blocks id2 | yes | yes |
| `depends_on` | id1 depends on id2 | yes | yes |
| `relates_to` | soft association | reciprocal | no |
| `duplicates` | id1 duplicates id2 | yes | no |
| `supersedes` | id1 supersedes id2 | yes | no |
| `discovered_from` | id1 was discovered while working id2 | yes | no |
| `caused_by` | id1 (a bug) was caused by the change/ticket id2 | yes | no |

Use `discovered_from` to record **provenance**: when working one ticket surfaces new work,
`create` the new ticket and `link <new> <parent> discovered_from` so the emergent-work
trail lives in the store.

`unlink <source> <target> [relation]` removes one link between the ordered pair. Without a
relation it removes the **most-recently-created** net-active link, preserving the historical
pair-scoped fallback; call it repeatedly to remove multiple links. With an explicit canonical
relation it removes exactly that relation's link and leaves the pair's other active relations
untouched.

**Direction and visibility.** A link is stored one-sided on the **source** ticket's record
(`deps`: outgoing edges only). `show` additionally renders the computed **`inbound_deps`**
list — every other ticket linking *to* the shown one, as `{from_id, relation, status}`
meaning "`from_id` \<relation\> this ticket" — so a ticket's blocked-ness (an inbound
`blocks`, or its own outgoing `depends_on`) is readable from a single `show`, consistent
with what `ready`/`next-batch` compute.

**Hierarchy escalation (blocking links only).** For `blocks` / `depends_on`, rebar requires
the two endpoints to be **siblings** — to share a parent. Comparability is *structural*: it
depends only on where the tickets sit in the parent hierarchy, never on their `ticket_type`,
so a `task` and a `story` that are both children of one epic hold a dependency directly.

When the endpoints do **not** share a parent, rebar escalates each one up to its own ancestor
that is a child of the two tickets' **nearest common ancestor**, and emits a
`REDIRECT: A→B promoted to …` note. So a dependency between two tasks in different stories
under one epic is recorded between those two stories. Tickets with **no parent** count as
siblings of each other, which means two roots link directly while a deep ticket linked across
trees escalates to its own root. A ticket can never block its own ancestor or descendant: such
a pair has no valid escalation and is rejected as a redundant link, since the hierarchy edge
already expresses the relationship. The non-blocking relations
(`relates_to` / `duplicates` / `supersedes` / `discovered_from` / `caused_by`) are recorded exactly as
given, with **no** escalation. One consequence: because a blocking link may be escalated to an
ancestor, `unlink` must target the **escalated (ancestor)** endpoint to remove it. The
escalation rule and the underlying `LINK` / `UNLINK` events are described in
[event-schema.md](event-schema.md).

**Auditing links written under an older rule (`rebar doctor`).** A `LINK` event is
durable and nothing re-resolves it on read, so a blocking edge recorded before the
escalation rule changed stays on disk exactly as written — and keeps feeding `ready`,
`next-batch` and the claim cascade. `rebar doctor` scans every net-active blocking
edge and asks the *current* resolver what it should be, reporting three kinds:

| kind | meaning | what `--repair` does |
| --- | --- | --- |
| `ancestor-blocking` | one endpoint is an ancestor of the other | unlinks it — the hierarchy edge already expresses the relationship, so there is no correct replacement |
| `mis-escalated` | the resolver returns a different pair than the one recorded | replaces it with the resolved pair |
| `unreadable` | an endpoint could not be reduced | reports only; never repaired |

It is read-only by default and exits **1** while any finding is outstanding, so it can
gate CI. `--repair` takes the write lock, refuses to run while a reconciler pass is in
flight, and force-writes the tag `pre-doctor-repair` at the tracker's pre-run OID.

Two safety properties are worth knowing before you run it. It writes the replacement
link **before** removing the stale one, so an interruption leaves *both* edges — a
superset the next run converges — rather than losing a dependency. Doctor currently checks
what `unlink`'s pair-scoped fallback would cancel; if that would cancel a *different*
relation, the pair is reported `unrepairable` and left alone rather than guessed at.

Re-resolving an already-escalated edge is a best-effort reconstruction: the recorded
event does not preserve the author's original endpoints. Individual intent is not
recoverable — the pre-repair tag is what makes a run reversible as a whole.

## Tags (convergent add/remove deltas)

Tags mutate via **add/remove deltas**, so two clones adding different tags both survive (no
whole-field clobber). The surface:

- `tag <id> <t>` / `untag <id> <t>` — single-tag add/remove (idempotent).
- `edit <id> --add-tag=a,b --remove-tag=c` — batch add/remove in one event.
- `edit <id> --set-tags=x,y` — replace the tag set. **It is compiled to a delta against
  the tags this clone has observed (add-wins): a concurrent tag another clone added that you
  haven't synced is NOT removed — so "set" is convergent, not an authoritative reset.**
  `--set-tags=""` clears the *observed* tags only. `--set-tags` cannot be combined with
  `--add-tag` / `--remove-tag` (error).
- `--tags` is **not** an `edit` flag (it would clobber); it remains only on `create`
  (genesis). The library/MCP `edit_ticket(tags=…)` is a deprecated alias for `set_tags`;
  prefer `add_tags` / `remove_tags` / `set_tags`.
- Tag names are trimmed; empty / whitespace-only / control-character names are rejected.

The convergent delta is carried by the `TAG_DELTA` event — its body, the add-wins conflict
rule, and the forward-compatibility rollout note (older clones preserve-and-ignore an
unknown event type) are documented in [event-schema.md](event-schema.md).

## File-impact scope

`file_impact` records the repository files a ticket expects to change for conflict-aware
scheduling and plan-review freshness. It has three persisted states:

| State | Stored fields | Meaning |
|-------|---------------|---------|
| **undeclared** | `file_impact: []`, `file_impact_scope: "undeclared"`, empty `no_file_impact_reason` | No scope has been recorded yet. |
| **paths** | non-empty `file_impact`, `file_impact_scope: "paths"`, empty `no_file_impact_reason` | The listed repository paths may change. |
| **none** | `file_impact: []`, `file_impact_scope: "none"`, substantive `no_file_impact_reason` | No repository files change; the reason explains why and where the output/evidence lives. |

Use `rebar set-file-impact <id> '[{"path":"docs/guide.md","reason":"document CLI behavior"}]'`
for documentation-only work and similarly list test paths for test-only work. Those are still
repository file changes, so they use **paths**, not **none**. Use
`rebar set-file-impact <id> --none "<reason>"` only when the ticket makes no repository file
changes at all. See [event-schema.md](event-schema.md) for the event fields and replacement
semantics.

## The project fields (`bridge_project` and `repos`)

A ticket carries two fields that place it in the store's many-to-many tracker-projects model
(see [ADR 0097](adr/0097-many-to-many-tracker-projects.md) and the
[user guide](user-guide.md#jira)):

- **`bridge_project`** is **tri-state**, and the three states are kept distinguishable across
  event replay by a *present-only* projection (a key-presence check, not a truthiness one):
  - **absent** (the seeded `None`) — the deliberate "legacy / not stated" sentinel; the ticket
    resolves to the mapping's `legacy_default`. A pre-mapping ticket, or any ticket created
    without a project flag, is in this state.
  - **`""`** (present, empty string) — an explicit **never-sync**: the ticket resolves to no
    project regardless of the default.
  - **a non-empty key** (e.g. `"DIG"`) — that project verbatim, the ticket's outbound sync
    target. It is a routing target, not validated against the mapping's key set, so an
    unknown key routes rather than erroring.
- **`repos`** is the list of repositories the ticket's project owns; it defaults to `[]` and,
  for an inbound-created ticket, is populated from the source project's `repos` entry in the
  mapping.

The mapping itself (which projects the store syncs, each project's `repos`, and the
`legacy_default`) is a committed store-level file managed with `rebar bridge projects
{list,set,remove}` — it is not a per-ticket concern; see the
[user guide](user-guide.md#jira) and [config.md](config.md).

## See also

- [event-schema.md](event-schema.md) — the append-only event bodies behind every concept
  above (`CREATE`, `STATUS`, `LINK`/`UNLINK`, `TAG_DELTA`), and the session_log type.
- [concurrency.md](concurrency.md) — optimistic concurrency, the parent-first cascade, and
  the convergent-delta invariants that make concurrent operation safe.
- [user-guide.md](user-guide.md) — the practical, human-facing walkthrough of driving
  tickets from the CLI.
