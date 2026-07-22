# ChatGPT agent guide

A ChatGPT session working on this repository may have the GitHub connector and repository
visibility but lack a local clone, direct Gerrit access, shell/network access, or a mounted
Rebar tracker. This guide teaches such a session to detect that environment, behave safely
within it, and avoid inventing tracker state.

## 1. Capability check (do this first)

Before doing anything else, check these six dimensions:

1. **Local checkout** — is there a working copy of this repository on disk?
2. **Native `rebar`/`rebar-mcp` binary** — is the CLI or MCP server installed and runnable?
3. **Tracker worktree** — is there a directory containing a valid `tickets` branch checkout
   (confirm with `rebar exists <known-id>` or equivalent)?
4. **Network/remote access** — can the session reach a git remote (fetch/clone)?
5. **GitHub connector** — does the session have GitHub repository access via a connector tool?
6. **Gerrit availability** — can the session reach the Gerrit code-review remote?

**Decision rule — native ticket creation is impossible** precisely when **both**:

- the tracker-worktree dimension fails (no accessible `tickets` branch checkout), **and**
- the network/remote dimension fails **or** the native `rebar`/`rebar-mcp` binary is unavailable.

In other words: the session cannot reach a real store through the CLI, the MCP server, or a
git-remote clone/fetch. Any other combination of capability failures does **not** trigger the
fallback below — use the live tracker.

## 2. GitHub is a read-only mirror; Gerrit is where code lands

The GitHub repository is a **read-only mirror** for code changes — all code changes land
through Gerrit, never a GitHub PR merge or a direct push to `main`. **GitHub Issues are not a
substitute for the Rebar tracker** — do not create GitHub Issues in place of Rebar tickets, and
do not treat GitHub Issues as tracker state.

## 3. When the live tracker is reachable

Use the native `rebar`/`rebar-mcp` commands exactly as any other agent would: **search before
create** (`rebar search <query>` or the `search` MCP tool) to avoid duplicates, then create
parent/child tickets natively (`create`, `link ... discovered_from`), claim before working, and
close only once acceptance criteria are demonstrably met. Never hand-author, modify, or delete
event files directly — the tracker is an **append-only** event log; every write must go through
the locked native write path (CLI, library, or MCP tool) so history and optimistic-concurrency
checks stay intact.

## 4. When native ticket creation is impossible (the fallback)

Do **not** claim a ticket was created. Instead:

1. Produce a **reviewable ticket payload** conforming to `src/rebar/schemas/export.schema.json`'s
   `required` fields, plus the usual description content:
   - `schema_version`: `1`
   - `ticket_id`: an agent-generated UUID4 (see below — this becomes the ticket's `source_id`
     on import, not a rebar-assigned id)
   - `ticket_type`, `title`: from the ticket content
   - `status`: `"open"`
   - `description`: including a `## Acceptance Criteria` block
   - `parent_id`: if applicable
2. Report the **exact missing capability** (which of the six dimensions failed) alongside the
   payload, so a human or a capable agent can pick it up.

This is the same shape produced by `rebar export` and consumed by `rebar import`, so a
maintainer can review it and optionally ingest it without hand-transcription. Because
`ticket_id` here is agent-generated (not rebar-assigned), re-running the same fallback payload
through `rebar import` later is idempotent by `source_id` — a no-op, not a duplicate.

## 5. The exceptional import mechanism

The **only** sanctioned path for turning a fallback payload into real tickets is `rebar import`
(or the library's `rebar.import_tickets()`) — the native, locked write path. **Never** hand-write
raw event files and **never** use a GitHub Contents API write to the `tickets` branch.

`rebar import`:

- Accepts **NDJSON** compiled-state projections conforming to the schema above (not raw events).
- Assigns **fresh local ids and HLC timestamps** to each created ticket, preserving source
  provenance via `source_id`/`source_created_at`/`source_author`.
- Is **idempotent by `source_id`** — an already-imported `source_id` is skipped, not
  re-created.

**Before invoking it:** confirm the target branch is current (`git fetch` + `rebar exists
<known-id>` against the intended tracker) and run `rebar import --dry-run` first. In dry-run
mode specifically, a record missing `ticket_id`/`ticket_type` is silently skipped — it
increments **no counter** and produces **no warning** at all (the `warnings` list is not
populated in the dry-run branch); only an already-imported record (its `source_id` already
present in the target) increments `skipped`. So a malformed record is invisible to
`would_create`/`skipped` individually — detect it instead by comparing
`would_create + skipped` against the total NDJSON record count: any shortfall is exactly the
count of malformed (missing `ticket_id`/`ticket_type`) records.

**After invoking it:** run `rebar validate` (or `rebar fsck`) against the tracker to confirm
store integrity.

## Summary checklist

- [ ] Ran the six-dimension capability check before acting.
- [ ] Treated GitHub as a read-only mirror; did not substitute GitHub Issues for the tracker.
- [ ] Used native search-before-create when the live tracker was reachable.
- [ ] Never hand-authored or mutated event files.
- [ ] Used the fallback payload (schema-conformant) and a transparent capability-gap report when
      native creation was genuinely impossible — never claimed persistence that didn't happen.
- [ ] Used only `rebar import` (never raw event injection or a Contents API write) for the
      exceptional ingest path, with a pre-flight `--dry-run` and a post-flight `rebar validate`.
