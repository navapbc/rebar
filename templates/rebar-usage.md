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
3. **Claim it — before you touch code** — `rebar claim <id> --assignee <you>` atomically
   moves the ticket `open → in_progress` and sets the assignee. If someone else already
   holds it you get a `ConcurrencyError` / exit 10 — pick another ticket, don't force.
   Never start editing against an unclaimed (`open`) ticket.
4. **Record provenance** — when a task uncovers more work, `rebar create …` then
   `rebar link <new> <parent> discovered_from`, so the emergent-work trail lives in the
   store.
5. **Finish** — `rebar transition <id> in_progress closed` when the acceptance criteria
   are met (optimistic-concurrency: pass the status you believe is current; a mismatch is
   exit 10). Reopen a closed ticket with `rebar reopen <id>`.

### Ticket hierarchy, links, tags, and the `idea` status

- **Hierarchy** is the `parent_id` chain (epic → story → task/bug), set with
  `create --parent <id>` / `edit --parent <id>` — **not** a link relation.
- **Links** carry a required relation: `blocks`, `depends_on`, `relates_to`, `duplicates`,
  `supersedes`, `discovered_from`. Blocking links (`blocks`/`depends_on`) are promoted up
  the hierarchy so a dependency lands between comparable levels.
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
- **Optional review gates** — a project can enable an LLM plan-review gate on claim and/or
  a completion-verifier on close (see your project's configuration); when on, a ticket
  must earn the relevant attestation before it can be claimed or closed.

### Working over MCP

When driving rebar from an LLM client, prefer the `rebar-mcp` tools: reads such as
`show_ticket`, `list_tickets`, `search`, `ready_tickets`, `next_batch`, `validate`, and
writes such as `create_ticket`, `claim_ticket`, `transition_ticket`, `comment_ticket`,
`link_tickets`, and `log_session` (writes are gated by `REBAR_MCP_READONLY`). The typed
read tools advertise an `outputSchema` you can rely on.
