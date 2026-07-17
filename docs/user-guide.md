# rebar user guide

rebar is an event-sourced ticket system backed by git. You drive it from the command
line with the `rebar` CLI, and every write is committed (and, when a remote is
configured, pushed) automatically — so your ticket activity is durable and shared the
moment you make it. This guide walks through the day-to-day loop. For internals see
[architecture.md](architecture.md) and [concurrency.md](concurrency.md).

Every command has `--help`; run `rebar <command> --help` to see its exact flags.

## The everyday loop

```
search / list ──▶ ready ──▶ claim ──▶ (work + comment) ──▶ transition closed
                                │
                     found new work? ──▶ create + link discovered_from
```

1. **Look first** so you don't duplicate work.
2. **Create** a ticket (or promote an `idea`) for the work.
3. **Claim** it — this is how you take ownership.
4. **Work**, recording progress as comments.
5. **Close** it when the acceptance criteria are met.

## Finding work

**Search** does a full-text, case-insensitive AND over titles, descriptions,
comments, and tags:

```sh
rebar search "login timeout"
```

You can also use field predicates inside the query (`status:`, `type:`, `priority:`,
`assignee:`, `tag:`, `parent:`), comma for OR within a field, and `-term` (or
`not:term`) to negate:

```sh
rebar search "status:open type:bug -flaky"
```

**List** filters structurally:

```sh
rebar list --status=open --type=task          # open tasks
rebar list --status=open,in_progress          # comma = OR
rebar list --has-tag=frontend --sort=-priority # highest priority first
rebar list --parent=<epic-id>                  # direct children of an epic
```

**Ready** shows only tickets whose blockers are all closed — i.e. actually workable
right now:

```sh
rebar ready
rebar ready --epic=<epic-id>
```

**Show** prints one or more tickets in full (description, comments, links, status):

```sh
rebar show <ticket-id>
```

## Creating tickets

```sh
rebar create task "Fix off-by-one in pager"
rebar create story "Add dark mode" --priority 2 --parent <epic-id>
rebar create bug "Crash on empty search" --description "..." --tags ui,regression
```

Types are `bug`, `epic`, `story`, `task`. Containment is the **parent** relationship
(`--parent <id>`), not a link — an epic contains stories, a story contains tasks/bugs.

Put a clear description on the ticket, including an **Acceptance Criteria** checklist,
so the quality gates pass (see below):

```markdown
## Acceptance Criteria
- [ ] Pager no longer skips the last row
- [ ] Regression test added
```

### The `idea` status — a parking lot for undesigned work

When you have a rough idea that isn't designed enough to work yet, capture it as an
`idea` rather than an `open` ticket. An `idea` is never scheduled as work (it never
appears in `ready`), so it won't get accidentally picked up:

```sh
rebar idea "Maybe cache the reducer output"
```

Promote it when it's ready to be worked:

```sh
rebar transition <id> idea open
```

## Claiming and transitioning

**Claim** atomically moves an `open` ticket to `in_progress` and sets the assignee.
This is how ownership is established — if someone else already claimed it you get a
non-zero exit (a normal "taken" signal, not a crash), so pick another ticket rather
than forcing:

```sh
rebar claim <ticket-id> --assignee alice
```

**Transition** moves a ticket between statuses. You can pass the current and target
status (rebar checks the current matches — a mismatch means someone else moved it),
or just the target to auto-detect:

```sh
rebar transition <id> in_progress closed   # explicit current -> target
rebar transition <id> closed               # auto-detect current
```

Statuses are `idea | open | in_progress | closed | blocked`. Closing a **bug**
requires a `--reason` prefixed with `Fixed:` or `Escalated to user:`:

```sh
rebar transition <id> in_progress closed --reason "Fixed: guard empty query"
```

**Reopen** moves a closed ticket back to open:

```sh
rebar reopen <ticket-id>
```

## Recording progress: comments, links, tags

Write progress, decisions, and findings back onto the ticket as **comments** so the
trail lives in the store:

```sh
rebar comment <id> "Root cause was an unclamped index; fix in pager.py."
```

**Link** two tickets with a relation (the relation is required):

```sh
rebar link <new-bug> <the-task> discovered_from   # provenance for emergent work
rebar link <a> <b> blocks                          # a blocks b
```

Relations: `blocks`, `depends_on`, `relates_to`, `duplicates`, `supersedes`,
`discovered_from`. Blocking links (`blocks` / `depends_on`) are promoted to a
comparable hierarchy level automatically. Remove a link with `rebar unlink <a> <b>`
(no relation argument; removes the most-recent link for that pair).

**Tag** and **untag** for lightweight labels; `rebar edit` changes fields:

```sh
rebar tag <id> needs-review
rebar untag <id> needs-review
rebar edit <id> --priority=1 --assignee=bob --add-tag=urgent
```

## Session logs — durable working notes

Session logs are verbose, searchable notes kept in the store (they never enter the
dependency graph or block anything). Append to the current log; the first append
creates one:

```sh
rebar session-log append "Spent the morning tracing the pager bug; see comment on <id>."
rebar session-log start --summary "Dark-mode implementation"   # rotate to a fresh log
rebar session-logs --limit 5                                    # newest first
```

The first `append` creates a log and records it as the **current** one via a local,
git-ignored pointer (`.rebar/current_session_log`); later appends go to that same log.
You rarely need `start`, because logs **auto-rotate per session**: the pointer stores a
session fingerprint alongside the log id — taken from the session-id resolver
(`REBAR_SESSION_ID`, then `CLAUDE_CODE_SESSION_ID`, then `SESSION_ID`) — so when a *new*
session's first `append` sees a pointer whose fingerprint differs, it rotates to a fresh
log automatically. Distinct agent sessions therefore get distinct logs with no manual
`start`. It degrades safely: when no session id is set at all (fingerprint absent), it
never rotates, so one continuous no-id session keeps appending to a single log. The
`session_log` type's store-level semantics (gate-exempt, graph/health-excluded, never
Jira-synced) are documented in [event-schema.md](event-schema.md).

## The quality gates as you experience them

rebar has a few self-checks you can run on demand. The **per-ticket** gates take a
ticket id and tell you whether a single ticket is well-formed enough to work or close:

```sh
rebar clarity-check <id>    # is the ticket shaped like dispatchable work? (score/verdict)
rebar check-ac <id>         # does it have an ## Acceptance Criteria block?
rebar quality-check <id>    # combined dispatch-readiness check
```

These are structural floor checks — a pass means "well-formed enough to dispatch,"
not "the content is good." The universal requirement is an `## Acceptance Criteria`
block with `- [ ]` checklist items; add per-type headings (file paths for tasks;
`## Why` / `## What` / `## Scope` for stories; Reproduction / Expected vs Actual for
bugs; `## Success Criteria` / `## Context` for epics) to score well.

The **review gates** are LLM-backed (they make a live model call and require the
optional agents extra + an API key):

```sh
rebar review-plan <id>          # review the plan before work starts
rebar verify-completion <id>    # check the ticket's completion criteria are met
```

`review-plan` sanity-checks a ticket's plan before it's worked;
`verify-completion` checks the acceptance/success criteria are demonstrably met.
Depending on project configuration these can gate claiming and closing — see
[plan-review-gate.md](plan-review-gate.md) for the full model.

Finally, **validate** is a repo-wide health check. It takes **no ticket id** — it
scans the whole store and returns a 1–5 health score with findings (orphans, cycles,
empty epics, and the like):

```sh
rebar validate
rebar validate --output json
```

## Concurrency, in one line

rebar is meant to be used by many people/clones at once. Status-changing operations
(`claim`, `transition`, `reopen`) are optimistic: if the ticket moved under you, you
get a clean "someone else changed it" signal (exit 10) rather than a silent clobber —
re-read and pick up from the current state. See [concurrency.md](concurrency.md).

## Jira

If your project syncs to Jira, tickets reconcile bidirectionally through
`rebar reconcile`. Setting that up is an operator task — see
[jira-sync-setup.md](jira-sync-setup.md).
