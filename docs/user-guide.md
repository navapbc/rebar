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

## Mutation confirmations

Every mutating verb (`create`, `idea`, `comment`, `link`, `unlink`, `revert`, `edit`, `tag`, `untag`, `archive`, `set-file-impact`, `set-verify-commands`, `attach-commits`, `session-log`, `transition`, `reopen`, `claim`) prints one confirmation line to standard output after a successful write. The line uses `<past-tense-verb> <args-summary>`. An idempotent operation prints `no change: <reason>` and exits 0.

```
$ rebar tag c50e-7326 perf
tagged c50e-7326-9cac-45e4: +perf
$ rebar tag c50e-7326 perf
no change: tag perf already on c50e-7326-9cac-45e4
```

These options may appear anywhere among the verb arguments. A bare `--` ends option parsing, so a comment body containing `--quiet` remains unchanged.

- `--quiet` or `-q` suppresses the text confirmation. Errors, exit codes, JSON output, and the machine-readable `REDIRECT` record from `link` remain available.
- `--output <text|json>` or `-o <mode>` selects text or JSON output. `create`, `idea`, `transition`, `claim`, and `reopen` retain their established JSON shapes. Other mutation commands return `{"outcome": "<verb-past>"|"noop", "subject", "detail"}`. This envelope remains unstable before version 1.0.

Combining `--quiet` with `--output json` still prints JSON. Confirmation lines use standard output. Warnings and logs use standard error. Scripts should parse `--output json` instead of text confirmations.

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

**Claim** atomically moves an `open` ticket to `in_progress` and sets the assignee to
the configured `ticket.default_assignee` (see [config.md](config.md)). An explicit
`--assignee` overrides it, and must be a Jira-resolvable identity — an email or accountId.
This is how ownership is established — if someone else already claimed it you get a
non-zero exit (a normal "taken" signal, not a crash), so pick another ticket rather
than forcing:

```sh
rebar claim <ticket-id>
```

**Transition** moves a ticket between statuses. You can pass the current and target
status (rebar checks the current matches — a mismatch means someone else moved it),
or just the target to auto-detect:

```sh
rebar transition <id> in_progress closed   # explicit current -> target
rebar transition <id> closed               # auto-detect current
```

Statuses are `idea | open | in_progress | blocked | closed | archived | deleted`. Closing a **bug**
requires a bounded `--class <value>` — one of `regression`, `plan_defect`,
`env_integration`, `flaky`, `preexisting`, `not_a_bug`, `duplicate`, `escalated`,
`obsolete`, `superseded`, `wontfix`, or `undetermined` (the escape hatch). The
value is folded into reduced state so `rebar show <bug> --output json` surfaces
`close_class`:

```sh
rebar transition <id> in_progress closed --class=regression
```

**Any** ticket type can close under an *administrative* disposition when the work
is not being completed: `--class=duplicate` / `--class=superseded` (backed by a
live `duplicates` / `supersedes` link) or `--class=obsolete` / `--class=wontfix`
(which require a free-text `--reason=<justification>`, folded into reduced state
as `close_reason`). These mint a signed disposition verdict instead of running
completion verification — see `docs/ticket-model.md` §"Administrative close
dispositions":

```sh
rebar transition <id> in_progress closed --class=wontfix --reason="descoped by epic Y"
```

### Recording what caused a bug

When closing a **bug**, use `--caused-by <id>` to record the ticket that introduced the defect. Rebar adds a directional, non-blocking `caused_by` link from the bug to that ticket.

```sh
rebar transition <bug> in_progress closed --class=regression --caused-by=<culprit-id>
```

An explicit value replaces a different `caused_by` link already recorded on the bug. If you omit the option, rebar preserves any existing link and may add one when repository history identifies a source. Failure to identify or record a source does not block the close.

Each `caused_by` link records its attribution as `explicit` or `derived`. Links created before attribution tracking report `unknown`. The bug-trend metrics expose these values separately.

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

### Passing untrusted or quoted text safely

When a sync remote is configured, a ticket write can be pushed after it is committed. Treat every body as content that can be published to collaborators and remote hosting.

Each write can update the `tickets` branch. Configure CI to exclude that branch from build triggers. Schedule any job that must read the store. See [concurrency.md](concurrency.md#outbound--push-on-every-write) for synchronization guidance.

**Never build a body from an unquoted shell command substitution.** The shell expands backticks and `$(...)` before rebar receives the text. Rebar cannot distinguish intended text from an environment dump or another accidental capture. Prefer a file that you have reviewed, and quote the substitution.

```sh
rebar comment <id> "$(cat notes.md)"     # submit a reviewed file
rebar comment <id> -- "$(cat notes.md)"  # allow a body that starts with a hyphen
```

Do not substitute environment dumps or command output that you have not inspected. Anything that you would not publish in an issue does not belong in a ticket body.

Rebar screens event content, including comments, descriptions, and edits, for complete credential patterns that meet length and entropy thresholds. Supported families include Anthropic, OpenAI, GitHub, Google, Atlassian, Slack, AWS, PyPI, Stripe, and PEM private keys. A match refuses the write. No event is stored. The error identifies the credential family, field, and line without displaying the matched value. Truncated examples such as `sk-ant-api03-...` and detection expressions remain writable when they do not meet the thresholds.

If the screen flags harmless text, a human operator can use the CLI override with a reason.

```sh
rebar comment <id> "<body>" --allow-secret-pattern="synthetic credential fixture"
rebar create bug "leak writeup" --description="..." --allow-secret-pattern="<reason>"
```

The override works on every write verb, including `comment`, `create`, `edit`, and `session-log`. It accepts only the `--allow-secret-pattern=<reason>` form. The event records the reason and the matched families. The override is available through the CLI and is not available through MCP.

Do not use the override to publish a credential. Remove the credential before retrying. If a credential may have been disclosed through another command or an earlier write, rotate it at its provider and remove it from every source before continuing.

**Link** two tickets with a relation (the relation is required):

```sh
rebar link <new-bug> <the-task> discovered_from   # provenance for emergent work
rebar link <a> <b> blocks                          # a blocks b
```

Relations are `blocks`, `depends_on`, `relates_to`, `duplicates`, `supersedes`, `discovered_from`, and `caused_by`. The `caused_by` relation records that a bug came from the change or ticket at the target. It is directional and does not block work. Blocking links connect tickets that share a parent. Across subtrees, rebar promotes `blocks` and `depends_on` links to the relevant children under the nearest common ancestor.

Remove the most recent link for a pair with `rebar unlink <a> <b>`. When a pair has several relations, remove one relation with `rebar unlink <a> <b> <relation>`.

**Tag** and **untag** for lightweight labels; `rebar edit` changes fields:

```sh
rebar tag <id> needs-review
rebar untag <id> needs-review
rebar edit <id> --priority=1 --assignee=alice@example.com --add-tag=urgent
```

## Session logs

Session logs are searchable working notes kept in the ticket store. They do not enter the dependency graph or block other tickets. Append an entry to the current log. Rebar creates a log when the session has none.

```sh
rebar session-log append "Spent the morning tracing the pager bug. See comment on <id>."
rebar session-log start --summary "Dark-mode implementation"   # rotate to a fresh log
rebar session-logs --limit 5                                    # newest first
```

Later appends from the same identified session use the same log. When rebar identifies a different session, its first append creates a new log. Use `start` when you want to rotate explicitly. Use `session-logs` to find recent logs.

The `session_log` type is exempt from lifecycle gates, excluded from graph health, and never synchronized to Jira. These properties are documented in [event-schema.md](event-schema.md).

### Ops error-sweep ledger

Debugging-orchestration sessions periodically sweep for AWS and GitHub Actions errors "since the last sweep." The last-sweep timeline is carried across sessions in **one long-lived `session_log` ticket used as an append-only ledger** — currently `tomophobic-stilllife-mayfly` (`b2dc-b1ab-0bd9-47f6`), tagged `ops-sweep`. A `session_log` is chosen deliberately: it is excluded from the default `list`, from `ready` / `next-batch`, and from graph-health reductions, and is never synced to Jira (see [event-schema.md](event-schema.md), "The session_log ticket type"), so an always-open ledger creates zero work-queue noise — unlike an open `task`/`bug`, which would pollute the queues forever.

**Find the last sweep with no prior knowledge:** search for the tag, open the ledger, read the newest comment.

```sh
rebar search "ops-sweep"                       # or: rebar search "error-sweep ledger"
rebar session-logs                             # lists session_log tickets, newest first
```

Each sweep appends a comment whose first line is a header:

```
SWEEP <ISO-8601 timestamp> | window <start>..<end>
```

The **newest** comment's header is the last sweep; the next sweep starts its window at that comment's `window_end`.

**Append a sweep entry by ticket id** — not with `session-log append`:

```sh
rebar comment <ledger-id> "SWEEP 2026-08-29T10:00-07:00 | window <start>..<end> ..."
```

`session-log append` is session-keyed and auto-rotates (a new session's first append starts a *new* log), which would scatter entries across many logs instead of keeping them in the one ledger; `rebar comment <ledger-id>` always targets the ledger itself.

**Link every ticket a sweep files** to the ledger so its relations enumerate everything the sweeps have surfaced:

```sh
rebar link <filed-ticket> <ledger-id> relates_to
```

A `session_log` accepts only the non-blocking relations (`relates_to`, `discovered_from`, `caused_by`); `blocks` / `depends_on` are refused on either endpoint.

**If the ledger is ever lost or archived, recreate it** — the `ops-sweep` tag plus search is the durable anchor, so the specific id is only a convenience pointer:

```sh
rebar session-log start --summary "OPS: error-sweep ledger (AWS + GitHub Actions)"
rebar tag <new-id> ops-sweep
```

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

If a review landed on **INDETERMINATE** because a finder unit degraded or the budget cap
shed some criteria, `rebar review-plan <id> --retry` resumes **only that latest review**:
it reuses the checkpointed successes (zero model calls for them) and re-runs only the missing
units under a fresh attempt budget. It acts only when the latest retained result is a
retryable INDETERMINATE with a current, versioned discovery journal; otherwise (a PASS/BLOCK,
a non-retryable indeterminate, or a missing/legacy/corrupt/stale journal) it **refuses before
any model call**, exits `2`, and prints the full-review remedy — run `rebar review-plan <id>`.
`--retry` is mutually exclusive with `--force`/`--status`/`--check`, compatible with
`--no-sign`, and — unlike `--status` (which only reads currency) and `sign-review` (which
re-certifies without re-running) — is the one path that pays for just the missing units. See
[plan-review-gate.md](plan-review-gate.md#resuming-exactly-the-latest-review--review-plan---retry).

`rebar claim <id> --force[=<reason>]` bypasses any enabled start-work gate (e.g.
plan-review) — not just plan-review specifically, but whatever gate is configured to run
on claim, now or in the future. `--force` is available on every surface — CLI, the library,
and MCP (`claim_ticket` takes a reason-bearing `force`); it is audited not by hiding it from
any surface but by the **absence of a signed attestation** (a forced claim records no
certification, so a project enforces the gate by checking for that certification in CI).

Finally, **validate** is a repo-wide health check. It takes **no ticket id** — it
scans the whole store and returns a 1–5 health score with findings (orphans, cycles,
empty epics, and the like):

```sh
rebar validate
rebar validate --output json
```

## Tracker footprint — pack size is not clone size

`rebar tracker-footprint [--fresh-clone] [--output text|json]` reports the store's
physical layers without applying a budget or changing the store. The default measures the
configured mounted tracker exactly as found. Because a mounted tracker may be a linked
worktree or use Git alternates, the report labels its object database `shared` and names the
reason instead of presenting shared Git bytes as a standalone-clone cost.

`--fresh-clone` resolves the configured `sync.remote` and `tracker.branch`, makes an
unfiltered single-branch/no-tags clone in a command-owned temporary directory, disables
Git's local hardlink optimization, measures it, and removes the temporary directory. It is
the reproducible choice when comparing independent clone residence:

```sh
rebar tracker-footprint --fresh-clone
rebar tracker-footprint --fresh-clone --output json
```

The layers are intentionally distinct:

- `pack` is the exact logical-byte sum of `.pack` files in the primary common Git object
  database; it excludes indexes and every checked-out file. Its `complete` flag is `true`
  when those pack files are the whole object store backing the checkout, and `false` when the
  checkout borrows objects from an alternate object database (an `alternates`-backed
  `git clone --shared`): a non-exclusive pack value must not be read as the whole object store.
- `checkout.logical_bytes` sums `lstat().st_size` once per non-directory pathname outside
  the tracker's root `.git`; `checkout.file_count` counts those pathnames.
- `checkout.allocated_bytes` uses `st_blocks * 512`, charging hard-linked storage once per
  `(st_dev, st_ino)`. `allocation_overhead_bytes` is allocated minus logical and may be
  negative. Platforms without `st_blocks` return a structured `unavailable`, never zero.
- `git_directory` measures the unique union of Git's worktree-specific and common
  directories, without nested-root or inode double counting.
- `whole_clone` combines checkout and Git-directory pathnames and inode-deduplicates
  allocation across both. Its `scope` says whether those Git bytes are standalone or shared.

The source block records the configured remote/branch, requested ref, measured ref, and tip.
Large values remain descriptive: size alone never changes the command's exit status and the
command is not run by writes, reconciliation, gates, ordinary metrics, or CI.

## Metrics — how the agent-driven loop is trending

`rebar metrics [--since <date>] [--until <date>] [--output json|text]` renders every metric
in the built-in registry over a date range, so you can ask "how is the agent-driven dev
loop trending?" without hand-rolling queries. With no date flags it reports the last 30
days (through today); either explicit ISO-8601 bound overrides its corresponding default.
It is read-only and derives everything from the durable event store, git, and the gate
sidecars.

Each metric is tagged with a **lens** — one of `agent_process` (attempts/rework/recovery
per ticket), `bug_trends` (bug close-class mix by month, time-to-close, open-bug age,
detection channels, caused-by fan-in, and the caused-by provenance split —
explicit/derived/unknown edge counts, where `unknown` is the pre-marker cohort),
`code_health` (module-size distribution and trend
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

### Module-size trend and cap-change events

`module_size_trend` and `cap_change_events` (`code_health`/`git`/`high`) derive the
module-size history straight from Git, independent of the scc-backed
`module_size_distribution`/`oversized_module_count` analyzer metrics above. Both walk the
commits reachable from `HEAD`, filter them inclusively by `--since`/`--until` on committer
date, and keep only **qualified** revisions — a commit whose tree has a positive-integer
`.github/module-size-limit.txt` blob AND at least one tracked `src/rebar/**/*.py` blob.
Qualified revisions are sorted oldest-to-newest by `(committer_timestamp, sha)`.

- `module_size_trend` reports every qualified revision when there are at most 50; beyond
  that it reports 50 samples at `round(i * (n - 1) / 49)` for `i = 0..49`, always
  including the first and last, plus the
  total `qualified_revisions` count and the `sampled_revisions` count actually returned.
  Each sample carries its commit `sha`, committer `timestamp`, the module-size cap **read
  from that same revision** (a later cap change never reinterprets an older sample), the
  tracked `module_count`, and `max_loc` — the largest module's raw newline count from its
  historical Git blob (the `wc -l` equivalent the CI gate uses), not a working-tree or
  analyzer measurement.
- `cap_change_events` compares the cap of every adjacent qualified revision **before**
  sampling and returns the ordered list of changes (`from`, `to`, `sha`, `timestamp` of the
  revision the cap changed to) plus `qualified_revisions`. A qualifying history with no cap
  change reports an empty `events` list — that is a real value, not `unavailable`.

Both report the standard `unavailable` shape, never a zero or empty placeholder, for: a
non-Git repository or a git failure, a date range with no commits, no positive-integer cap
blob found anywhere in range, no qualifying `src/rebar/**/*.py` modules found anywhere in
range, or fewer than two qualified revisions overall. Each reason names its category so an
`unavailable` result is diagnosable without re-running the command.

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

When you run a **single-ticket** CLI command (`show`, `comment`, `edit`, `transition`,
`reopen`, `deps`, and friends) against a ticket whose live claim is held by
**another session**, rebar prints an advisory `WARN:` line to **stderr** naming the holder — the
command's stdout payload and exit code are untouched, so it never breaks a pipeline.
Bulk commands (`list`, `ready`, `next-batch`, `search`, …) and `claim` do not warn.
Set `warnings.cross_session = false` (default on) to disable the notice.

## A dirty tickets tracker: `fsck` names it, `doctor --repair` heals it

A crash mid-compaction or an interrupted sync can leave the tickets tracker's working
tree dirty, which wedges auto-commit. `rebar fsck` classifies that state into three
finding classes (each carried in `--output json` with a per-class count and path list):

- `TRACKER_DIRTY_DELETION` — a tracked store artifact deleted from the working tree or
  index but restorable from the tracker's HEAD. Counted as an issue.
- `TRACKER_DIRTY_LEFTOVER` — an untracked, regenerable compaction leftover: a
  `*-SNAPSHOT.json`, or a `*.retired` whose retired-source is already folded (the source
  exists at HEAD, or the `.retired` file itself is preserved on the sync remote). Counted
  as an issue. A `.retired` that is preserved nowhere else is deliberately **not**
  classified — it may be the only copy of an event.
- `TRACKER_DIRTY_TMP_EVENT` — an orphaned `.tmp-event-*` temp file. Report-only and never
  counted or auto-touched: a live one belongs to an in-flight append. In `--output json` it
  appears in `issues[]` with `counted: false`, so it is excluded from `issue_count`.

`STATUS_FORK_RESOLVED` belongs to that same report-only class: `fsck` still emits it (in
the text report and in `--output json`'s `issues[]`, carrying the ticket id and
`counted: false`), but it is never counted, so it cannot by itself push `fsck`'s exit code
to 1 or inflate `--output json`'s `issue_count`. A resolved status fork is not damage — the
reducer already resolved the cross-clone race deterministically by UUID, and
`status_fork_resolutions` is permanent derived state that survives compaction. Counting it
would pin a busy store's `fsck` at exit 1 forever with nothing to repair.

`--output json`'s `issue_count` is the COUNTED subset of `issues[]` (the items with
`counted: true`) and therefore AGREES with the exit code: 0 when the run exits 0, ≥ 1 when
it exits 1. Every report-only kind — `PUSH_PENDING`, `STATUS_FORK_RESOLVED`,
`TRACKER_DIRTY_TMP_EVENT`, and any `WARN:` line — is carried in `issues[]` with
`counted: false` and never contributes to `issue_count`. A consumer that wants the old
"every emitted line" total can still compute `len(issues)`. An uninitialized or absent
tracker is reported as a single counted `not_initialized` issue, so its JSON payload is
distinguishable from a clean store's empty `issues[]`.

`rebar doctor --repair` heals the first two classes and then reconverges the store:

1. Before the first mutation it records a backup ref `refs/rebar-doctor/<utc-ts>` at the
   tracker's HEAD (the same envelope pattern as `tracker-maintenance`).
2. Under one short write-lock window it restores deletions via `git checkout HEAD --` and
   **moves** (never deletes) leftovers into
   `<git-common-dir>/reconverge-quarantine/<utc-ts>/`.
3. After releasing the lock it runs the store's reconverge (which takes the write lock
   itself), so local and remote history union-merge back together.

Orphaned `.tmp-event-*` files are printed with a `manual` repair status and left
byte-identical; inspect and remove them by hand only when you know the append that wrote
them is dead. On a clean tracker `doctor --repair` makes zero changes and records no
backup ref.

## `doctor` also audits your `[mapping]` config

`rebar doctor` diagnoses the hand-edited `[mapping]` seam so a misconfigured mapping can't
silently drift a Jira reconcile. It reports two classes, and — like the rest of `doctor` —
folds them into its exit code, but keeps them OUT of `--repair` (mapping findings are
report-only):

- **Offline checks (always run, no Jira, no credentials).** Any invalid config is an
  **error** finding (and a non-zero exit): a block that fails to parse (a non-integer
  `hierarchy`, a malformed vocabulary list), a mapped value that falls outside a declared
  vocabulary, or a syncable ticket type left with no sync decision. The finding carries the
  `MappingConfigError` message verbatim so the offending key is named. An all-empty
  `[mapping.projects.<KEY>]` block — a likely stub — is a softer **warning** (it does *not*
  fail the exit code).
- **Live drift (best-effort, degrades).** When the optional `jira-datacenter` extra is
  installed and a `JIRA_PAT` is set in the environment (env-only, never a config key),
  `doctor` reuses the read-only probe to compare each
  project's configured status / type / link target values against what Jira actually
  exposes, and reports any configured value Jira no longer has as a drift **error**. When
  the extra is absent, credentials are unresolved, or the probe is slow or unreachable, the
  check degrades to a single **`unavailable`** finding (a zero exit) — the same convention
  `rebar metrics` uses — so `doctor` stays fully portable and never blocks on Jira.

## Archived tickets: maintenance scopes to the active store

`rebar archive` folds the ticket's entire live log into a SNAPSHOT **inline, right before
writing the ARCHIVED event** — a terminal fold that bypasses the incremental compaction
gates (threshold 0, fold horizon of now), so an archived ticket never carries an unfolded
tail. Archiving an already fully-folded ticket writes no new SNAPSHOT, and a failed fold
aborts the archive. This fold is operation-linked: it runs wherever `archive` runs, with no
CI or scheduler dependency.

Because archived tickets are settled and folded, the store-walking maintenance commands
skip them by default, so their cost tracks store *activity*, not store *history*:

- `rebar fsck`'s per-ticket checks walk **active** tickets only; pass `--include-archived`
  to restore the full historical walk (works with `--output json` too).
- `rebar compact-all` selects among active tickets only; `--include-archived` also sweeps
  archived ones — the migration door that folds tickets archived before the archive-time
  fold existed.

A directory is skipped only when its `.archived` marker exists **and** the event log
confirms net archival (an ARCHIVED event with no REVERT targeting it). The marker alone
never decides, so a stale marker — e.g. left behind by a reverted archive — cannot hide a
ticket from `fsck` or the compaction sweep.

## Jira

If your project syncs to Jira, use `rebar bridge preview` to show proposed Jira
changes and `rebar bridge sync` to apply them. `rebar bridge pause
REASON` to stop scheduled reconciliation, and `rebar bridge resume` to restart it.
`preview` and `sync` accept `--only IDS` or `--except IDS`; `sync` also accepts
positive `--max-changes N`. Canonical preview runs without writer locks and emits a
deterministic, field-level audit manifest without applying changes. Canonical sync retains a
comparable manifest for both capped and uncapped runs; a capped run also records the complete
deferred remainder.

**Syncing several tracker projects.** One store can bridge any number of Jira projects. The
set is a committed mapping owned by the store (`.bridge_state/projects.json` on the tickets
branch), managed with `rebar bridge projects`:

```sh
rebar bridge projects list                    # print the mapping as JSON
rebar bridge projects set DIG --repos digit   # add/replace project DIG's repos (replace semantics)
rebar bridge projects remove DIG              # drop a project from the sync list
```

`set` uses replace semantics for the named key's `--repos` list (a comma-separated list of
repositories that project's tickets belong to); `remove` exits non-zero if the key is absent.
Each ticket carries a `bridge_project` (its sync target) and `repos`; a ticket with no
`bridge_project` resolves to the mapping's `legacy_default` (see
[the ticket model](ticket-model.md#the-project-fields-bridge_project-and-repos)). A store
created before this feature seeds a mapping from its configured `jira.project` (recording it
as the `legacy_default`, with an empty sync list) automatically at the next `rebar init` /
`rebar fsck --repair` (see [migrations.md](migrations.md)); the design rationale is [ADR
0097](adr/0097-many-to-many-tracker-projects.md).

**Upgrade note for mixed-version fleets.** Once a store actually syncs more than one project,
it stamps a `multi-project-bridge` capability into its committed compatibility record. A
binary too old to know that capability **fails closed** on the store — it refuses to sync it
rather than applying a single-project model it cannot honor. **The remedy is to upgrade that
binary** to one that provides `multi-project-bridge` (every current binary does). A store that
still syncs a single project stays readable by older binaries, so the fail-closed gate affects
only genuinely multi-project stores.

`rebar bridge status` reads the reconciler's durable last-pass, pause, and live-lock witnesses.
Use `--json` for automation, `--target ENVIRONMENT_ID` to select the expected producer, and
`--max-age 2h` only when age should make an otherwise successful pass stale. Without
`--max-age`, no implicit age threshold applies. Healthy, paused, and running exit zero; foreign,
failed, stale, and never-run exit nonzero. The older `rebar bridge-status` spelling remains a
hidden compatibility alias; `purge-bridge` remains retired.

The legacy top-level `rebar reconcile` adapter is removed. Use `rebar bridge preview` for live
Jira-vs-local proposed changes, `rebar bridge sync` for writes, `rebar bridge fsck` for offline
binding/integrity audit, and `rebar bridge status` for operational state. Direct argument-less
`python -m rebar_reconciler` still means live synchronization, and its supported rollout modes
remain `dry-run`, `bootstrap-strict`, `bootstrap-throttle`, and `live`; direct
`--mode reconcile-check` now rejects. The engine-only `--filter-local-ids` post-filter remains
available on that direct legacy route for live-DC harness scoping. The legacy `jira-onboard`,
`bridge-probe`, and `bridge-fsck` spellings remain available as compatibility aliases.

`rebar bridge fsck` is audit-only with one exception: `rebar bridge fsck --repair` prunes
reverse bindings that have no forward entry (`store_integrity` findings of kind
`reverse_missing_forward`). Such a key is otherwise unremovable and makes the binding-drift
canary alert indefinitely on a benign fault, masking real integrity problems behind a constant
non-zero count. The prune acts on exactly the audited finding set and refuses — writing
nothing — when any other integrity kind is present, when an orphaned key is tombstoned in
`bindings-retired.json`, or when the store changed since the audit. Deletion goes through the
binding store's own atomic write, the forward map is left untouched, and each run appends a
durable record (actor, deleted keys, before/after counts) to
`rebar-bridge-repair-audit.jsonl` in the tracker's git directory. It exits `0` when it healed
the store or found nothing to do, `1` when a guard refused, and `2` on operational failure.

Repository-scheduled synchronization is portable across GitHub Actions, Jenkins, and GitLab.
Each provider prepares a full-history checkout and `.tickets-tracker`, then invokes
the installed `rebar bridge run` command. The adapter retains the established `MODE` values
(`reconcile-check`, `dry-run`, `bootstrap-strict`, `bootstrap-throttle`, and `live`) while
routing new work through the noun-based `bridge preview` / `bridge sync` commands. It requires
the Jira variables, `REBAR_ENV_ID`, `BRIDGE_RUN_ID`, and the bridge bot name/email before the
pass starts; a shallow checkout is rejected because it cannot reconcile ticket history safely.
This shared CI adapter keeps the `reconcile-check` profile spelling for provider compatibility;
that profile now invokes canonical preview rather than the removed top-level reconcile route.

For an explicit one-off selection, use `rebar bridge run --profile dry-run`. Provider
templates normally omit `--profile` and supply the established `MODE` environment
compatibility boundary instead.

Python and MCP callers have the same noun-based machine operations:
`bridge_preview`, `bridge_run`, `bridge_sync`, `bridge_status`, `bridge_pause`,
`bridge_resume`, `bridge_check_access`, and `bridge_fsck`. Their results are
schema-backed dictionaries. `bridge_run(profile=...)` accepts the same compatibility profile as
the installed CLI and returns captured streams without printing, so it is safe for MCP stdio.
`bridge_preview` cannot write; `bridge_run` and `bridge_sync` are explicitly mutating. The
legacy library, MCP, and top-level CLI `reconcile` interfaces are removed; use `bridge_preview`
for proposed changes, `bridge_sync` or `bridge_run` for mutating callers, `bridge_fsck` for
offline binding/integrity audit, and `bridge_status` for operational state. Interactive
`bridge setup` remains CLI-only. Setting up Jira is an operator task — see
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
rebar bridge preview       # inspect proposed Jira changes
rebar bridge sync          # apply the staged synchronization
rebar bridge pause "maintenance window"  # stop scheduled reconciliation
rebar bridge resume        # resume scheduled reconciliation
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

Design rationale, the shared Jira-family layer, and the Data Center support horizon (DC goes
read-only on 28 March 2029, so this adapter is a deliberately time-boxed investment) are in
[ADR 0055](adr/0055-jira-family-sub-seam.md).
