# Phase 5 — Ticketization (work product: an epic + child tickets)

> Read this at the start of Phase 5. Input = the approved / refined item set. File it into **the
> repository's tracker, following the project's conventions** — infer the tracker, ticket vocabulary,
> and workflow from the repo guidance you read in Phase 0 (do not hardcode a specific tracker). Use
> the project's own commands.

## The epic

Create an epic titled **`Janitor Cleanup <YYYY-MM-DD>`**. Its body summarizes the cleanup and holds
**one acceptance-criteria checklist item per finding** the plan remediates, so closing the epic
validates every finding was addressed.

## The child tickets

- **At least one child per approved remediation** (a grouped remediation is one ticket listing all
  findings it closes). You are authorized to **split a large remediation into multiple children, each
  with a discrete deliverable**.
- Each child carries: the finding(s) + citations, the remediation approach/end-state, the
  `cascade_flag` note (if any), and its own acceptance criteria.
- Author child descriptions to the project's readiness gates (e.g. an `## Acceptance Criteria`
  checklist) so they pass validation.
- Link children to the epic via the project's parent/child mechanism (not a loose relation).

## Registry re-confirmation (batched, after ticket creation)

Phase 2 handed off the set of **known-fine entries that went stale this run but re-verified as still
fine**. Present them now as a **single batched list** — *"these blessed entries went stale (their code
changed / spread / entered a hotspot) and still look fine; re-confirm? [y/n each]."*

- **Re-confirm** → update that entry's `confirmed_on`, `content_fingerprint`, `blessed_instances`, and
  `hotspot_at_confirmation` to the current values (this write is the human confirmation).
- **Decline** → leave the entry stale; it shields nothing and will be re-verified again next run
  (compute-only, no attention) until re-confirmed or deleted.

Do this **after** ticket creation so it doesn't interrupt the approval flow. (Entries whose findings
surfaced as survivors and became tickets need no action — they retire via pattern-gone GC once fixed.)

## Close out

- Restate in your own message text: the epic id/link, the child count, any items the user rejected,
  and any registry entries added (Phase 4) or re-confirmed (above).
- Enrich `.rebar-janitor/report-<YYYY-MM-DD>.md` with the outcome (which survivors became tickets,
  which were rejected, registry changes).
- Do **not** edit code — the tickets are the deliverable. The edits happen later, when someone works
  a ticket. (Janitor's principle: cleanup is completed *between* features; approved work should be
  implemented before the next run, not left to accumulate as a backlog.)
