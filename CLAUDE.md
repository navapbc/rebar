# rebar — agent guide

rebar is an event-sourced ticket system + Jira reconciler exposed as a Python
library (`import rebar`), a CLI (`rebar`), and an MCP server (`rebar-mcp`), all
over one git-backed store. This guide is for **agents** driving rebar (especially
over MCP). For internals see `docs/architecture.md`, `docs/event-schema.md`, and
`docs/concurrency.md`.

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
`ticket_deps`, `ready_tickets`, `next_batch`, `clarity_check`, `check_ac`,
`quality_check`, `validate`, `get_file_impact`, `get_verify_commands`, `fsck`,
`summary`, `list_epics`, `bridge_fsck`, `reconcile` (dry-run by default). The
typed read tools advertise an `outputSchema` (a documented, validated return
shape) drawn from the canonical JSON Schemas — see
[docs/output-schemas.md](docs/output-schemas.md).

**Writes (gated by `REBAR_MCP_READONLY=1`):** `create_ticket`,
`transition_ticket`, `claim_ticket`, `reopen_ticket`, `comment_ticket`,
`edit_ticket`, `link_tickets`, `unlink_tickets`, `tag_ticket`, `untag_ticket`,
`archive_ticket`, `compact_ticket`, `set_file_impact`, `set_verify_commands`.

There is no `init` over MCP (operator bootstrap only). `reconcile` `live` mode
additionally requires `REBAR_MCP_ALLOW_RECONCILE_LIVE=1`. Both env gates accept
any case-insensitive truthy value (`1`/`true`/`yes`, whitespace tolerated);
anything else (including unset) is off.

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

Plus, for all types: a description ≥ ~200 chars and at least one bullet/checklist
line. A ticket missing the `## Acceptance Criteria` checklist fails both gates
regardless of how rich the rest of the description is.

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
The **`REBAR_PUSH`** env var tunes this (default `always`): `async` pushes in the
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
```
