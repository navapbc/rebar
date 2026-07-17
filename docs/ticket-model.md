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
  **no** completion-verifier / signature / bug-close-reason gate (an undesigned idea has
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

## Linking (the six relations + hierarchy promotion)

`link <id1> <id2> <relation>` **requires** a relation. There are six:

| Relation | Meaning | Directional? | Can create a cycle? |
|----------|---------|--------------|---------------------|
| `blocks` | id1 blocks id2 | yes | yes |
| `depends_on` | id1 depends on id2 | yes | yes |
| `relates_to` | soft association | reciprocal | no |
| `duplicates` | id1 duplicates id2 | yes | no |
| `supersedes` | id1 supersedes id2 | yes | no |
| `discovered_from` | id1 was discovered while working id2 | yes | no |

Use `discovered_from` to record **provenance**: when working one ticket surfaces new work,
`create` the new ticket and `link <new> <parent> discovered_from` so the emergent-work
trail lives in the store.

`unlink <source> <target>` takes **no** relation argument — it is pair-scoped and removes
the **most-recently-created** link between that ordered pair, one per call. If a pair has
multiple links, call `unlink` repeatedly.

**Hierarchy promotion (blocking links only).** For `blocks` / `depends_on`, rebar promotes
the link endpoints up the parent hierarchy so the dependency lands between tickets at a
comparable level (epic ↔ epic, story ↔ story, task/bug ↔ task/bug), emitting a
`REDIRECT: A→B promoted to …` note when it does. This is why a blocking link you point at a
child ticket can land on its epic. The non-blocking relations
(`relates_to` / `duplicates` / `supersedes` / `discovered_from`) are recorded exactly as
given, with **no** promotion. One consequence: because a blocking link may be promoted to an
ancestor, `unlink` must target the **promoted (ancestor)** endpoint to remove it. The
promotion rule and the underlying `LINK` / `UNLINK` events are described in
[event-schema.md](event-schema.md).

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

## See also

- [event-schema.md](event-schema.md) — the append-only event bodies behind every concept
  above (`CREATE`, `STATUS`, `LINK`/`UNLINK`, `TAG_DELTA`), and the session_log type.
- [concurrency.md](concurrency.md) — optimistic concurrency, the parent-first cascade, and
  the convergent-delta invariants that make concurrent operation safe.
- [user-guide.md](user-guide.md) — the practical, human-facing walkthrough of driving
  tickets from the CLI.
