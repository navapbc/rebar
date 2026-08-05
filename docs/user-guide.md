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

**Show** prints one or more tickets in full (description, comments, links, status). The
default view includes the computed `inbound_deps` — tickets linking *to* the shown one
(`{from_id, relation, status}`) — so "is this ticket blocked?" is answerable from one `show`:

```sh
rebar show <ticket-id>
```

## Creating tickets

```sh
rebar create task "Fix off-by-one in pager"
rebar create story "Add dark mode" --priority 2 --parent <epic-id>
rebar create bug "Crash on empty search" --description "..." --tags ui,regression
```

Automated filers can stamp the detection channel: set `REBAR_DETECTED_BY=<source>` in
the environment, or pass `--detected-by <source>` (the flag overrides the env var).
The value is normalized to lowercase; unknown tokens are accepted verbatim.

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
requires a bounded `--class <value>` — one of `regression`, `plan_defect`,
`env_integration`, `flaky`, `preexisting`, `not_a_bug`, `duplicate`, `escalated`,
or `undetermined` (the escape hatch). The value is folded into reduced state so
`rebar show <bug> --output json` surfaces `close_class`:

```sh
rebar transition <id> in_progress closed --class=regression
```

### Blame-Hunt Advisory (bug close → `caused_by`)

On a **bug** close, rebar best-effort draws a `caused_by` link from the bug to the
change/ticket that most likely introduced it. It finds the fixing commit (the one whose
message references this bug), blames the *pre-fix* state of the bug's recorded
`file_impact` files, and — if a strict majority of the blamed lines belong to one commit
that itself resolves to a ticket — links the bug to that culprit. It is purely advisory:
an ambiguous or absent culprit is silently skipped and never blocks the close. Set it
explicitly with `--caused-by <id>` (which overrides the git-blame auto-derivation):

```sh
rebar transition <bug> in_progress closed --class=regression --caused-by=<culprit-id>
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
`discovered_from`. Blocking links (`blocks` / `depends_on`) connect tickets that
share a parent; across sub-trees they escalate automatically to the children of
the two tickets' nearest common ancestor. Remove a link with `rebar unlink <a> <b>`
(removes the most-recent link for that pair) or `rebar unlink <a> <b> <relation>`
(removes exactly that relation's link when the pair holds several).

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
bugs; `## Context` for epics) to score well. A container's acceptance criteria must describe
substantive outcomes, and its child decomposition must show that the children collectively
deliver every criterion without overlapping responsibilities.

The **review gates** are LLM-backed (they make a live model call and require the
optional agents extra + an API key):

```sh
rebar review-plan <id>          # review the plan before work starts
rebar verify-completion <id>    # check the ticket's completion criteria are met
```

`review-plan` sanity-checks a ticket's plan before it's worked;
`verify-completion` checks the acceptance criteria are demonstrably met.
Depending on project configuration these can gate claiming and closing — see
[plan-review-gate.md](plan-review-gate.md) for the full model. `review-plan` **fast-fails
without running the LLM** when the ticket isn't claimable yet — status `closed`/`idea`/`blocked`,
or `open` but blocked by an unclosed dependency (returns an unsigned `INDETERMINATE`, exit `2`);
pass `--force` to review it anyway.

**A passing plan review SIGNS by default.** The attestation — not the printed findings — is
the review's durable product, and it is what the claim gate consumes, so `review-plan` signs
one automatically on a non-blocking `PASS`. You do not ask for the signature; you ask to skip
it:

```sh
rebar review-plan <id>            # PASS -> signs an attestation (the default)
rebar review-plan <id> --no-sign  # run the review, deliberately sign nothing
```

Some outcomes are **never** signed, whatever you pass: a `BLOCK` or `INDETERMINATE` verdict,
and a degraded run (one whose LLM tier resolved abnormally) — none of those is a certifiable
`PASS`. An unsigned result leaves the claim gate unsatisfied, so `rebar claim` still fails.

If a review computed a genuine `PASS` but the signature was lost (it failed to persist), you
do **not** need to pay for the review again — `rebar sign-review <id>` re-signs from the
recorded `REVIEW_RESULT` sidecar with **no LLM call**. It refuses if the plan changed since
the review or if the recorded verdict was not a signable `PASS`.

`rebar claim <id> --force[=<reason>]` bypasses any enabled start-work gate (e.g.
plan-review) — not just plan-review specifically, but whatever gate is configured to run
on claim, now or in the future. `--force` is CLI-only: it is not exposed over MCP (an MCP
client always goes through the configured gate).

Finally, **validate** is a repo-wide health check. It takes **no ticket id** — it
scans the whole store and returns a 1–5 health score with findings (orphans, cycles,
empty epics, and the like):

```sh
rebar validate
rebar validate --output json
```

## Metrics — how the agent-driven loop is trending

`rebar metrics [--since <date>] [--until <date>] [--output json|text]` renders every metric
in the built-in registry over a date range, so you can ask "how is the agent-driven dev
loop trending?" without hand-rolling queries. With no date flags it reports the last 30
days (through today); either explicit ISO-8601 bound overrides its corresponding default.
It is read-only and derives everything from the durable event store, git, and the gate
sidecars.

Each metric is tagged with a **lens** — one of `agent_process` (attempts/rework/recovery
per ticket), `bug_trends` (bug close-class mix by month, time-to-close, open-bug age,
detection channels, caused-by fan-in), `code_health` (module-size distribution and trend
vs the locked cap, churn, refactor-to-addition ratio, cap-change events), `delivery`
(commit cadence), and `gate_economics` (LLM cost-per-accepted-change, first-pass
verification, env-diagnosis intervals) — plus a `source` and a `confidence` label.
`bug_trends` mixes flow dimensions (respect `--since`/`--until`, filtered on close time)
and stock dimensions (point-in-time snapshots that deliberately ignore the range). A
metric whose signal has not
accrued yet reports a structured `{"unavailable": {"reason", "accruing_since"}}` rather
than a zero, and lights up automatically as data arrives — so **treat `unavailable` as
"no data", never as zero**. Backfilled/classified values (`source=backfill_classified`,
`confidence=classified`) are kept segregated from the authoritative structural series
(`is_authoritative()` is False for them), so a fuzzy backfill never contaminates a
high-confidence trend. The library/registry side of this surface is documented in
[reuse-surface.md](reuse-surface.md); the exact CLI syntax is in
[cli-reference.md](cli-reference.md).

### Code-health analyzer installation and fallback

Analyzer-backed code-health metrics need all three parts of this installation:

- `pip install "nava-rebar[metrics]"` installs the optional Python dependency, **lizard**.
- Install **scc** separately and ensure its executable is on `PATH` for LOC and module-size
  metrics.
- Install **jscpd** separately and ensure its executable is on `PATH` for duplication metrics.

The `[metrics]` extra contains only lizard; scc and jscpd are external executables, never pip
dependencies of rebar. The analyzers map to their signals as follows: scc → LOC/module size,
lizard → complexity, and jscpd → duplication. If an analyzer is absent or fails, its affected
metric returns a structured `Unavailable`; rebar does not fabricate a zero and the whole
`rebar metrics` command does not crash. Git- and event-derived metrics remain independently
available.

> **Contributing to rebar itself?** `make install` already provisions lizard, and the concrete
> per-platform install commands for scc and jscpd are in
> [`local-dev-env.md`](local-dev-env.md) under "Code-health analyzers".

## Concurrency, in one line

rebar is meant to be used by many people/clones at once. Status-changing operations
(`claim`, `transition`, `reopen`) are optimistic: if the ticket moved under you, you
get a clean "someone else changed it" signal (exit 10) rather than a silent clobber —
re-read and pick up from the current state. See [concurrency.md](concurrency.md).

## Jira

If your project syncs to Jira, tickets reconcile bidirectionally through
`rebar reconcile`. Setting that up is an operator task — see
[jira-sync-setup.md](jira-sync-setup.md).

### Jira Cloud vs. Jira Data Center

`[tool.rebar.reconciler].backend` chooses which Jira the reconciler drives. `"jira"` (the
default) is **Jira Cloud**, over the ACLI subprocess — nothing extra to install.
`"jira-datacenter"` is **self-hosted Jira Server / Data Center** (8.14+), over the
`pycontribs/jira` client with Personal Access Token auth.

Data Center needs an opt-in extra; a Cloud-only install pulls in no new dependency.

```bash
pip install 'nava-rebar[jira-datacenter]'
```

```toml
[tool.rebar.reconciler]
backend  = "jira-datacenter"
base_url = "https://jira.internal.example.gov"   # https is required (see below)

[tool.rebar.jira]
project = "REB"                                   # env override: JIRA_PROJECT
```

```bash
export JIRA_PAT=...        # the Personal Access Token — env-only, never a config key
rebar reconcile --dry-run  # inspect before enabling live sync
```

**Least privilege — use a dedicated service account, never an admin token.** Mint the PAT on
a service account whose permissions are scoped to **only the projects rebar reconciles**, with
just the rights the reconciler uses: **Browse Projects, Create Issues, Edit Issues, Add
Comments, Link Issues**. An admin-level token gives a sync bridge far more reach than it needs
and turns any reconciler defect into an instance-wide one. `JIRA_PAT` is read from the
environment and is **not** accepted from a committed config file, so the credential cannot be
checked into a repo by accident; the transport never logs it, and a missing `JIRA_PAT` fails
with an error naming the variable rather than falling back to anonymous access.

**`labels` must be on the project's Create and Edit screens.** rebar writes a `rebar-id:<id>`
label to correlate a Jira issue with its local ticket, so a project whose screens omit `labels`
will reject that write. rebar does **not** discover or populate custom required fields — the
supported answer is to add `labels` to those screens (or reconcile a project that already has
it), matching the boundary mature Jira integrations draw.

**A `createmeta` pre-flight is deliberately out of scope, and that is the mature answer rather
than a gap.** Jira's `/rest/api/2/issue/createmeta` can enumerate a project's required fields,
so an obvious-looking fix is to query it and populate whatever it reports. rebar does not, and
the precedent is explicit: **Sentry's Jira integration states it does not support custom
required fields — "The only required fields supported are those that are pre-populated by
Sentry."** A sync bridge cannot invent a meaningful value for an arbitrary required custom
field; guessing one writes plausible-looking wrong data into the tracker, which is worse than
refusing. Configuring the screen is a one-time admin action with a correct answer, so that is
where the boundary sits.

If the write does fail, **the created issue is retained, not deleted**: it is left bound as
*pending* and the next reconcile pass retro-attaches the label deterministically. Expect an
issue that exists but is briefly unlabelled, plus a `BRIDGE_ALERT` naming the failure. Jira's
own message here — *"Field 'x' cannot be set. It is not on the appropriate screen, or
unknown"* — is misleading roughly half the time: it also appears when the workflow property
`jira.permission.createclone.denied` is set, and when a value is simply malformed on a field
that *is* on the screen.

Three further DC-only keys, all under `[tool.rebar.reconciler]`:

| Key | What it does |
|---|---|
| `allow_insecure` | **Cleartext only if you must.** `base_url` must be `https`; a non-TLS URL is rejected at config load unless you set `allow_insecure = true`, which logs a warning naming the cleartext risk. It governs the URL **scheme only** — it never relaxes certificate verification. Intended for a loopback test instance, not a production bridge. |
| `ca_bundle` | Path to a CA bundle for an **internal-CA or self-signed** certificate — the supported answer to a verification failure. Certificate verification is never disabled (the standard `REQUESTS_CA_BUNDLE` env var works too); disabling it would make the `https` requirement theatre. |
| `resolved_statuses` | The workflow state names that count as resolved, for the absence probe. Defaults to `["Resolved", "Done", "Cancelled"]`. Set it if your self-hosted workflow names its resolved states differently — otherwise a resolved issue is misclassified. |

Design rationale, the shared Jira-family layer, and the Data Center support horizon (DC goes
read-only on 28 March 2029, so this adapter is a deliberately time-boxed investment) are in
[ADR 0055](adr/0055-jira-family-sub-seam.md).
