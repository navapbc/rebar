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
`reconcile` (dry-run by default).

**Writes (gated by `REBAR_MCP_READONLY=1`):** `create_ticket`,
`transition_ticket`, `claim_ticket`, `reopen_ticket`, `comment_ticket`,
`edit_ticket`, `link_tickets`, `unlink_tickets`, `tag_ticket`, `untag_ticket`,
`archive_ticket`, `compact_ticket`, `set_file_impact`, `set_verify_commands`.

There is no `init` over MCP (operator bootstrap only). `reconcile` `live` mode
additionally requires `REBAR_MCP_ALLOW_RECONCILE_LIVE=1`.

## Quality gates

Before dispatching/closing, self-check with `clarity_check` (score/verdict),
`check_ac` (has Acceptance Criteria), `quality_check` (dispatch readiness), and
`validate` (overall). Record `set_file_impact` (the `{path,reason}` array that
`next_batch` uses to avoid scheduling file-conflicting tickets together) and
`set_verify_commands` (DD-level verification) so downstream scheduling and
verification work.

## Library quick reference

```python
import rebar
tid = rebar.create_ticket("task", "title", return_alias=True)   # -> {"id","alias"}
rebar.claim(tid["id"], assignee="me")                            # raises ConcurrencyError if taken
hits = rebar.search("login")                                     # replay-derived list
rebar.link(child, parent, "discovered_from")
rebar.transition(tid["id"], "in_progress", "closed")
```
