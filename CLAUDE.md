# rebar — agent guide

rebar is an event-sourced ticket system + Jira reconciler exposed as a Python
library (`import rebar`), a CLI (`rebar`), and an MCP server (`rebar-mcp`), all
over one git-backed store. This guide is for **agents** driving rebar (especially
over MCP). For internals see `docs/architecture.md`, `docs/event-schema.md`, and
`docs/concurrency.md`.

> **Record your work in rebar, not in scratch notes.** Before starting, `search`/
> `list` for an existing ticket; if none fits, `create` one and capture the plan
> (and its acceptance criteria) in the description. As you work, write progress,
> decisions, and emergent findings back as `comment`s on the ticket (and `create`
> + `link … discovered_from` for new work you uncover), so the plan and its trail
> live in the store — durable, shared on every write, and visible to other agents
> — rather than in ephemeral TODOs or commit messages alone. Close with
> `transition <id> in_progress closed` when the acceptance criteria are met.

## The parallel-agent workflow

```
list / search ──▶ ready ──▶ next-batch ──▶ claim ──▶ (work) ──▶ transition closed
                                              │
                                   discovered new work? ──▶ create + link discovered_from
```

1. **Find work** — `search <query>` (full-text over titles/descriptions/comments/
   tags) or `list --status=open`; `ready` returns tickets whose blockers are all
   closed; `next-batch <epic>` returns a conflict-aware unblocked batch (uses each
   ticket's recorded file-impact).
2. **Grab work atomically** — `claim <id> --assignee <you>`. This is the
   concurrency primitive: it moves an **open** ticket to `in_progress` and sets the
   assignee in one atomic step. If another agent already claimed it you get a
   **ConcurrencyError / exit 10** — do not retry the same ticket; pick another.
   Never hand-roll claim as `transition`+`edit` (that races).
3. **Record provenance** — when work surfaces new work, `create` the ticket and
   `link <new> <parent> discovered_from` so the emergent-work trail is captured.
4. **Finish** — `transition <id> in_progress closed` (optimistic-concurrency:
   pass the status you believe is current; a mismatch is exit 10). Use `reopen`
   to move a closed ticket back to open.

## Optimistic concurrency (read this)

State-dependent ops (`transition`, `claim`, `reopen`) re-read the ticket under a
lock and **reject with exit 10 / `ConcurrencyError`** if the actual status no
longer matches your expectation. That is normal under parallelism — it means
someone else moved the ticket. Re-read (`show`) and decide; never force.

Cross-machine double-claims cannot be *prevented* (there is no cross-client lock
by design) but they *converge*: the event log merges as a union and replay
resolves the STATUS fork deterministically by UUID, so every clone agrees.

## MCP tool set

**Reads (always available):** `show_ticket`, `list_tickets`, `search`,
`recent_session_logs`, `ticket_deps`, `ready_tickets`, `next_batch`,
`clarity_check`, `check_ac`,
`quality_check`, `validate`, `get_file_impact`, `get_verify_commands`,
`verify_signature`, `fsck`, `summary`, `bridge_fsck`, `reconcile` (dry-run by
default). The
typed read tools advertise an `outputSchema` (a documented, validated return
shape) drawn from the canonical JSON Schemas — see
[docs/output-schemas.md](docs/output-schemas.md).

> `list_epics` is **deprecated** — it is now a thin wrapper over `list_tickets`.
> Use `list_tickets(ticket_type="epic", status="open,in_progress",
> blocking_state="unblocked", min_children=N, with_children_count=True)` for epics
> and `list_tickets(ticket_type="bug", priority=0)` for P0 bugs.

**Writes (gated by `REBAR_MCP_READONLY=1`):** `create_ticket`,
`transition_ticket`, `claim_ticket`, `reopen_ticket`, `comment_ticket`,
`edit_ticket`, `link_tickets`, `unlink_tickets`, `tag_ticket`, `untag_ticket`,
`archive_ticket`, `compact_ticket`, `set_file_impact`, `set_verify_commands`,
`log_session` (capture helper — appends a verbose entry to the current
`session_log`, creating one on first use),
`sign_manifest` (HMAC-signs a manifest of verified steps with the environment key;
`verify_signature` certifies it).

There is no `init` over MCP (operator bootstrap only). `reconcile` `live` mode
additionally requires `REBAR_MCP_ALLOW_JIRA_SYNC=1` (deprecated alias
`REBAR_MCP_ALLOW_RECONCILE_LIVE`). Both env gates accept
any case-insensitive truthy value (`1`/`true`/`yes`, whitespace tolerated);
anything else (including unset) is off.

## Session logs (`session_log` ticket type)

`session_log` is a first-class ticket type for **verbose, durable, agent-facing
logs** stored in the rebar store and surfaced later by keyword. It is
deliberately kept out of the dependency-graph / store-health hot paths so its
large bodies never tax the operations that run constantly during the
parallel-agent workflow. Its behavior differs from work tickets:

- **Gate- and lifecycle-exempt.** `clarity_check` / `check_ac` / `quality_check`
  treat it as exempt (always pass), and `validate` never flags it
  (orphan/empty/etc.). It **cannot** be `claim`ed or `transition`ed; `show`,
  `comment`, and `edit` work normally.
- **Visibility — searchable, hidden from `list`.** Included in keyword `search`
  and in single-ticket `show`, and listed by `recent_session_logs` /
  `list_tickets(ticket_type="session_log")`, but **excluded** from default
  `list` and from `ready` / `next_batch` / `deps` / `validate` (the graph/health
  compiles), so log size/count never affects those.
- **Non-blocking links only.** `relates_to` / `discovered_from` are allowed (so a
  log can reference the work it documents); `blocks` / `depends_on` are refused on
  either endpoint, and logs never enter the dependency graph.
- **Never synced to Jira.** `reconcile` excludes `session_log` (it is in the
  reconciler's `EXCLUDED_SYNC_TYPES` and absent from the local→Jira type map), and
  it never appears in `bridge_fsck`.
- **Title convention (guidance, NOT enforced).** Titles should carry a short
  summary of the work (not merely a date/time/session id); nothing validates this.

**Capture helper.** Rather than hand-assembling `create` + `comment`, use the
helper: library `rebar.append_session_log(entry, *, summary=…, relates_to=…,
discovered_from=…)`, CLI `rebar session-log append "<entry>"` (and
`rebar session-log start --summary=…` to rotate to a fresh log), or the
write-gated MCP `log_session(entry)`. The first call creates one `session_log`
(titled by `summary`) and records it as the current log via a **local,
git-ignored** pointer (`.rebar/current_session_log`); subsequent calls append to
that same log. Retrieve recent logs with `rebar.recent_session_logs(limit=5)` /
`rebar session-logs [--limit=<n>]` / the MCP `recent_session_logs` tool (newest
first, default 5).

**LLM agent operations (optional, gated):** `review_ticket(ticket_id, reviewer_id,
graph)` runs a tool-using LLM agent that reviews a ticket (or its graph) and
returns a `review_result` (`{findings[], …}`). `verify_completion(ticket_id,
graph)` runs the **completion-verifier** agent that checks a ticket's completion
requirements (acceptance/success/close criteria, definitions of done; for bugs,
that the bug is resolved) are demonstrably met by the implementation and returns a
`completion_verdict` (`{verdict: PASS|FAIL, findings[], …}`; on FAIL each finding
cites the failing criterion + a source-code citation). Both are **disabled unless
`REBAR_MCP_ALLOW_LLM=1`** (they make a live, billable LLM call) and need the
`nava-rebar[agents]` extra + `ANTHROPIC_API_KEY`. Part of the optional `rebar.llm`
framework (CLI: `rebar review` / `rebar verify-completion`; library:
`rebar.llm.review_ticket` / `rebar.llm.verify_completion`) — see
[docs/llm-framework.md](docs/llm-framework.md).

> **Completion-verification close gate (optional).** When
> `verify.require_completion_verification_for_close=true` (default off; **on for
> this project**), closing a work ticket runs `verify_completion` first: a **FAIL**
> verdict (or an unavailable LLM) **blocks** the close (fail-closed), and on **PASS**
> the verdict is HMAC-**signed** onto the ticket (the trustworthy attestation, only
> meaningful under the MCP server's environment key; CI verifies it). `--force-close`
> closes without verifying or signing — so a **closed-without-signature** ticket is the
> durable signal that validation did not pass. It is an *alternative* to the signature
> gate (`require_signature_for_close`), not composed with it.

## Quality gates

The **per-ticket** gates each take a ticket id and self-check a single ticket
before you dispatch/close it: `clarity_check` (score/verdict), `check_ac` (has an
Acceptance Criteria block), `quality_check` (dispatch readiness). Separately,
`validate` is a **repo-wide** tracker-health check — it takes **NO ticket id**
(passing one errors); it scans the whole store and returns an overall health
score (1-5) bucketed into critical/major/minor/warning findings (e.g. orphaned
tasks, cycles, cross-epic child deps). Use the per-ticket gates on the ticket
you're working; use `validate` for store-level health.

The per-ticket gates are **structural floor checks**, not semantic scoring:
`clarity_check` is a heading/length/bullet heuristic, so it confirms a ticket is
*shaped* like dispatchable work — it can't judge whether the content is actually
good. Treat a pass as "well-formed enough to dispatch," not "high quality."

### Ticket template the gates enforce

Author descriptions to this per-type matrix so the gates pass first time. An
**`## Acceptance Criteria`** block with `- [ ]` checklist items is the universal
floor — `check_ac` requires it on **every** type, and `clarity_check` will not
pass without it either (the two gates share one vocabulary). The per-type
headings below are what `clarity_check` additionally rewards:

| Ticket type | Required (all)            | Type-specific headings clarity rewards            |
|-------------|---------------------------|---------------------------------------------------|
| `task`      | `## Acceptance Criteria`  | file paths (e.g. `src/…/x.py`)                    |
| `story`     | `## Acceptance Criteria`  | `## Why`, `## What`, `## Scope`                   |
| `bug`       | `## Acceptance Criteria`  | `## Reproduction Steps`, Expected vs Actual       |
| `epic`      | `## Acceptance Criteria`  | `## Success Criteria`, `## Context`               |
| `session_log` | *(none — gate-exempt)*  | n/a (always passes the gates; see Session logs)   |

Plus, for all types **except `session_log`**: a description ≥ ~200 chars and at
least one bullet/checklist line. A ticket missing the `## Acceptance Criteria`
checklist fails both gates regardless of how rich the rest of the description is.
(`session_log` is exempt from all of this — see the Session logs section above.)

Record `set_file_impact` (the `{path,reason}` array that `next_batch` uses to
avoid scheduling file-conflicting tickets together) and `set_verify_commands`
(DD-level verification) so downstream scheduling and verification work.

## Module-size policy (when editing rebar itself)

rebar is built to be edited by agents that load a unit whole. **Target 200–500
LOC per file; soft cap 800.** When a unit grows past the cap, split it **only
along call-graph seams that already exist** (extract a cluster of functions that
already call each other) — never mechanically to hit a number, and **never create
files < 100 LOC** by splitting. Prefer **deleting** oversized bash via the
bash→Python strangler-fig migration over carving it into more bash. The current
over-cap offenders and their planned remedies are tabulated in
`docs/architecture.md` (a warn-only CI report flags new ones).

## Navigating the codebase (when editing rebar itself)

This checkout has the **Serena** MCP server configured (LSP-backed, Pyright over
`src/rebar`) for *semantic* code navigation. **Prefer its symbol tools over
`grep`** when finding or following references — `find_symbol`,
`find_referencing_symbols`, `get_symbols_overview`, and symbol-precise edits
(`replace_symbol_body`, `insert_after_symbol`). It resolves "who calls / imports
this?" reliably (definitions + references, not text matches), which is exactly
what cross-cutting refactors (e.g. the bash→Python migration's importer sweeps)
need. Serena's tools load at **session start**; if they're absent, the server is
registered in local MCP config — verify with `claude mcp get serena` (re-add with
`claude mcp add serena -- uvx --from git+https://github.com/oraios/serena serena
start-mcp-server --context ide-assistant --project "$(git rev-parse --show-toplevel)"`).
Its per-developer cache/config lives in the git-ignored `.serena/`. `grep`/the
search tools remain the fallback when Serena is unavailable or for non-symbol
(text/comment/string) searches.

## Ticket hierarchy (parent/child)

Containment (epic→story→task/bug) is the **`parent_id`** hierarchy, **not** a `link` relation:
parent work to the epic/story it belongs to with `create --parent <id>` or `edit --parent <id>`
(see `rebar create --help`). Don't attach an epic's workstreams with a `depends_on`/
`discovered_from` link — **parent** them, or they aren't its children (the hierarchy is what
`ready`/`next-batch`/`validate`/the completion gate's child-closure check operate on).

## Linking (relations + hierarchy promotion)

- `link <id1> <id2> <relation>` **requires** a relation. The six relations:
  `blocks`, `depends_on`, `relates_to`, `duplicates`, `supersedes`,
  `discovered_from`.
- `unlink <source> <target>` takes **no** relation argument — it is pair-scoped
  and removes the **most-recently-created** link between that ordered pair (one
  per call). If a pair has multiple links, call `unlink` repeatedly.
- **Hierarchy promotion (blocking links only).** For `blocks`/`depends_on`, rebar
  promotes the link endpoints up the parent hierarchy so the dependency lands
  between tickets at a comparable level (epic↔epic, story↔story,
  task/bug↔task/bug), emitting a `REDIRECT: A→B promoted to …` note when it does.
  This is why a blocking link you point at a child ticket can land on its epic.
  Non-blocking relations (`relates_to`/`duplicates`/`supersedes`/
  `discovered_from`) are linked exactly as given, with **no** promotion.
  Consequence: because a blocking link may be promoted to an ancestor, `unlink`
  must target the **promoted (ancestor)** endpoint to remove it.

## The store shares every write immediately

Every write (`create`/`edit`/`transition`/`claim`/`link`/…) auto-commits its
event to the `tickets` branch **and** auto-pushes to `origin/tickets` when an
`origin` remote exists — so your local ticket activity (including test tickets)
propagates to the shared remote immediately. Push is best-effort: no remote means
no push, and a push failure never fails the write (the commit stays local and
diverged). `fsck` reports `PUSH_PENDING` when the local branch is ahead of origin.
The **`REBAR_SYNC_PUSH`** env var tunes this (default `always`; deprecated alias
`REBAR_PUSH`): `async` pushes in the
background so per-write network latency doesn't serialize a batch claim, and `off`
keeps commits local — both still surface `PUSH_PENDING` via `fsck` (see
`docs/concurrency.md`).

## Library quick reference

```python
import rebar
tid = rebar.create_ticket("task", "title", return_alias=True)   # -> {"id","alias"}
rebar.claim(tid["id"], assignee="me")                            # raises ConcurrencyError if taken
hits = rebar.search("login")                                     # replay-derived list
rebar.link(child, parent, "discovered_from")
rebar.transition(tid["id"], "in_progress", "closed")

import rebar.llm                                                  # optional [agents] extra
review = rebar.llm.review_ticket(tid["id"], "ticket-quality")    # -> review_result {findings[], …}
```
