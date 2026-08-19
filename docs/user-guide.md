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

Every mutating verb (`create`, `idea`, `comment`, `link`, `unlink`, `revert`, `edit`,
`tag`, `untag`, `archive`, `set-file-impact`, `set-verify-commands`, `attach-commits`,
`session-log`, `transition`, `reopen`, `claim`) confirms its result on stdout with one
kubectl-style line — `<past-tense-verb> <args-summary>` on a successful write,
`no change: <reason>` on an idempotent no-op (exit 0 in both cases):

```
$ rebar tag c50e-7326 perf
tagged c50e-7326-9cac-45e4: +perf
$ rebar tag c50e-7326 perf
no change: tag perf already on c50e-7326-9cac-45e4
```

Two global flags are extracted for these verbs at the top-level router
(position-independent within the verb's arguments; tokens after `--` are never
consumed, so a comment body containing `--quiet` survives verbatim):

- `--quiet` / `-q` suppresses the text confirmation only — errors, exit codes, JSON
  output, and `link`'s machine-readable REDIRECT record are untouched.
- `--output <text|json>` / `-o <mode>`: verbs that already accepted `--output`
  (`create`, `idea`, `transition`, `claim`, `reopen`) keep their pre-existing JSON
  shapes; the newly-covered verbs emit one uniform mutation envelope
  `{"outcome": "<verb-past>"|"noop", "subject", "detail"}` — **pre-1.0 UNSTABLE**
  (the field set may still change before 1.0). `--quiet` + `--output json` still
  prints the JSON.

Confirmation lines go to stdout; warnings and logs go to stderr. Scripts should
parse `--output json`, never the text lines.

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

Statuses are `idea | open | in_progress | closed | blocked`. Closing a **bug**
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

### Passing untrusted or quoted text safely — and the secret screen

The store **auto-pushes**, so a comment body is published the moment it is written. Two
rules follow.

(A third consequence is for whoever runs your CI: every write pushes the `tickets`
branch, so a pipeline that builds all branches runs once per comment. Configure CI so
that branch triggers **no** workflow, and put anything that must read the store on a
schedule — see [concurrency.md](concurrency.md#outbound--push-on-every-write).)

**Never build a body from an unquoted shell command substitution.** On 2026-08-03 a
session ran the equivalent of `rebar comment <id> "$(env)"` and published a full
environment dump carrying seven live credentials. GitHub push protection then rejected
`refs/heads/tickets`, and **every** session's writes queued local-only for hours — one
comment took down store sharing for everyone. Prefer a file you have read, and quote it:

```sh
rebar comment <id> "$(cat notes.md)"     # a file you control — safe
rebar comment <id> -- "$(cat notes.md)"  # ... and `--` if the body may start with "-"
rebar comment <id> "$(env)"              # NEVER: publishes your whole environment
```

Backticks and `$(...)` are expanded by *your shell* before rebar sees anything, so rebar
cannot tell an intended body from an accidental credential dump. Anything you would not
paste into a public issue does not belong in a body.

**rebar now refuses secret-bearing bodies at the write seam.** Every event write —
comment, description, edit — is screened for live credential shapes (Anthropic, OpenAI,
GitHub, Google, Atlassian, Slack, AWS, PyPI, Stripe, PEM private keys). A match refuses
the write: nothing lands, and the error names the credential family, the field, and the
line — never the value itself. Only *live* shapes fire (full length plus an entropy
floor), so writing *about* a credential — a truncated placeholder like `sk-ant-api03-...`
or a detection regex — is not refused, and filing a security bug still works.

If the screen is wrong, override it with a reason:

```sh
rebar comment <id> "<body>" --allow-secret-pattern="synthetic fixture, not a live key"
rebar create bug "leak writeup" --description="..." --allow-secret-pattern="<reason>"
```

The flag works on every write verb (`comment`, `create`, `edit`, `session-log`, …) and
takes the `--flag=<reason>` form only, so a reason can never be omitted. The reason and
the bypassed families are recorded on the event, so a forced write is auditable and
distinguishable from a clean one. The override is CLI-only — it is a human operator's
judgment call and is deliberately not exposed over MCP. Forcing a **genuine** credential
through reproduces the original outage for every session, so the flag is for false
positives, not for convenience.

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
rebar edit <id> --priority=1 --assignee=alice@example.com --add-tag=urgent
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
  counted or auto-touched: a live one belongs to an in-flight append.

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

The established `rebar reconcile` adapter remains available: no arguments still mean
dry-run, every historical `--mode` value is retained, and `--filter-local-ids` keeps
its write-only filtering semantics. Direct argument-less `python -m rebar_reconciler`
still means live synchronization, including its historical uncapped LIVE tally/no-manifest
behavior. The canonical spellings are `rebar bridge setup`, `rebar bridge check-access`,
`rebar bridge fsck`, and `rebar doctor`. The legacy `jira-onboard`, `bridge-probe`, and
`bridge-fsck` spellings remain available as compatibility aliases.

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
This shared CI adapter does not remove or change direct legacy `rebar reconcile` and
`python -m rebar_reconciler` entrypoints.

For an explicit one-off selection, use `rebar bridge run --profile dry-run`. Provider
templates normally omit `--profile` and supply the established `MODE` environment
compatibility boundary instead.

Python and MCP callers have the same noun-based machine operations:
`bridge_preview`, `bridge_run`, `bridge_sync`, `bridge_status`, `bridge_pause`,
`bridge_resume`, `bridge_check_access`, and the existing `bridge_fsck`. Their results are
schema-backed dictionaries. `bridge_run(profile=...)` accepts the same compatibility profile as
the installed CLI and returns captured streams without printing, so it is safe for MCP stdio.
`bridge_preview` cannot write; `bridge_run` and `bridge_sync` are explicitly mutating. The
legacy library and MCP `reconcile(mode=...)` interfaces remain supported with the dry-run
default and historical mode/return/error contracts. Interactive `bridge setup` remains CLI-only.
Setting up Jira is an operator task — see
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
