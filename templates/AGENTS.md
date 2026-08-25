# AGENTS.md — <YOUR PROJECT>

This is a starter `AGENTS.md` for a project that adopts **rebar** to track its work.
`AGENTS.md` is the cross-vendor guidance file read natively by many coding-agent
harnesses, so one file teaches all of your agents. Copy this file to your repository root
and fill in the two `PLACEHOLDER` sections below with your project's own commands and flow.
The delimited **rebar-usage** region further down is generated from rebar's canonical
source — do not hand-edit it; re-sync it instead (see the marker comment).

## Build, test & run — PLACEHOLDER (fill in for your project)

Replace this section with how agents build, test, and run *your* project. For example:

- Install / bootstrap: `PLACEHOLDER — e.g. make install`
- Run the tests: `PLACEHOLDER — e.g. make test`
- Lint / typecheck: `PLACEHOLDER — e.g. make lint && make typecheck`

## Reviewing & landing changes — PLACEHOLDER (fill in for your project)

Replace this section with how a change gets reviewed and landed in *your* project — your
branch/PR or review flow, the checks a change must pass, and who approves it. rebar tracks
the *work*; your own process lands the *code*.

<!-- BEGIN rebar-usage (generated; do not edit) -->
## Driving rebar (the ticket workflow)

This project tracks work in [rebar](https://github.com/navapbc/rebar), an event-sourced
ticket system exposed as a Python library (`import rebar`), a CLI (`rebar`), and an MCP
server (`rebar-mcp`) over one git-backed store. Track **all** work here — not in ad-hoc
TODOs, scratch notes, or commit messages alone.

### The loop (do this for every task)

```
list / search ──▶ ready ──▶ claim ──▶ (work) ──▶ transition closed
                              │
                   discovered new work? ──▶ create + link discovered_from
```

1. **Look first** — `rebar search <query>` (full-text over titles/descriptions/comments/
   tags) or `rebar list --status=open`; `rebar ready` returns tickets whose blockers are
   all closed. Do this before starting so you don't duplicate or clobber existing work.
2. **Create a ticket for new work** — `rebar create <type> "<title>"` (types: `task`,
   `story`, `bug`, `epic`). Capture the acceptance criteria in the description under an
   `## Acceptance Criteria` heading with `- [ ]` checklist items.
3. **Claim before editing.** Run `rebar claim <id>`. It atomically moves the ticket from `open` to `in_progress`. When `ticket.default_assignee` is configured, claim applies that identity. When the setting is empty, the claimed ticket remains unassigned. Pass `--assignee` only for an explicit override. In a Jira-reconciled store, use an email or accountId that Jira can resolve. If another session holds the ticket, rebar returns `ConcurrencyError` with exit 10. Select another ticket and do not force the claim.
4. **Record provenance** — when a task uncovers more work, `rebar create …` then
   `rebar link <new> <parent> discovered_from`, so the emergent-work trail lives in the
   store.
5. **Finish** — `rebar transition <id> in_progress closed` when the acceptance criteria
   are met (optimistic-concurrency: pass the status you believe is current; a mismatch is
   exit 10). Reopen a closed ticket with `rebar reopen <id>`.

### Ticket hierarchy, links, tags, and the `idea` status

- **Hierarchy** is the `parent_id` chain (epic → story → task/bug), set with
  `create --parent <id>` / `edit --parent <id>` — **not** a link relation.
- **Links.** Each link has one relation from `blocks`, `depends_on`, `relates_to`, `duplicates`, `supersedes`, `discovered_from`, or `caused_by`. Blocking links using `blocks` or `depends_on` are promoted through the hierarchy so a dependency connects comparable levels.
- **Tags** mutate via convergent add/remove deltas (`tag`/`untag`, or
  `edit --add-tag/--remove-tag/--set-tags`), so concurrent clones adding different tags
  both survive.
- **`idea`** is a first-class status for captured-but-undesigned work: it is
  structurally unclaimable and excluded from `ready`, but fully listable/searchable and
  promotable to `open` later. Capture one with `rebar idea "<title>"`.

### Session logs (durable working notes)

`rebar session-log append "<note>"` keeps verbose, searchable working notes in the store
(they never enter the dependency graph or block anything). The first append creates a log;
distinct sessions auto-rotate to distinct logs. Retrieve recent ones with
`rebar session-logs`.

### Quality gates you'll experience

- **Per-ticket structural gates** — `rebar clarity-check <id>`, `rebar check-ac <id>`, and
  `rebar quality-check <id>` confirm a ticket is *shaped* like dispatchable work (the
  universal floor is an `## Acceptance Criteria` block with `- [ ]` items). A pass means
  "well-formed enough to dispatch," not "the content is good."
- **Repo-wide health** — `rebar validate` scores the whole store (orphans, cycles,
  cross-epic child deps) and takes no ticket id.
- **Optional review gates.** A project can require an LLM plan review before claim, a completion verification before close, or both. When plan review is required, run `rebar review-plan <id>` and obtain a PASS before claim. Review prerequisites before dependents because a dependency change invalidates the dependent review attestation. Consult the project configuration for enabled gates.

### Working over MCP

When driving rebar from an LLM client, prefer the `rebar-mcp` tools: reads such as
`show_ticket`, `list_tickets`, `search`, `ready_tickets`, `next_batch`, `validate`, and
writes such as `create_ticket`, `claim_ticket`, `transition_ticket`, `comment_ticket`,
`link_tickets`, and `log_session` (writes are gated by `REBAR_MCP_READONLY`). The typed
read tools advertise an `outputSchema` you can rely on.

**When an MCP server is configured, route the tracker operations named throughout this guide
(`search`/`list`/`ready`/`next-batch`, `create`/`claim`/`transition`/`comment`/`link`, and any
enabled review gate) through these tools rather than the bare local `rebar` CLI.** The local
CLI shown in the steps above is the carve-out/fallback: it stays the tool for local-code ops
that need the checkout in hand (running tests, validation), and it is the fallback when no MCP
server is configured.
<!-- END rebar-usage -->

## Project-specific notes — PLACEHOLDER (optional)

Add anything specific to your repository below (directory layout, conventions, domain
context). Keep the generated rebar-usage region above untouched — edit rebar's canonical
source and re-sync if the ticket-workflow guidance itself needs to change.
