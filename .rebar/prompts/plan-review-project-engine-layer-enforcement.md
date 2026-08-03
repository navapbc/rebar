---
schema_version: 1
title: Engine-layer enforcement
description: Find plans that place a new validation, guard, or invariant inside a single interface surface (a CLI command, the MCP server, or one library facade) while the other ingress paths bypass it, suggesting the fix should be at the shared engine seam.
execution_mode: single_turn
category: plan-review-criterion
dimension: project-invariants
---
You are reviewing a rebar plan for **engine-layer enforcement**. rebar is reached through
three independent ingress paths: (1) CLI commands in `src/rebar/_commands/`, (2) the MCP
server (`src/rebar/mcp_server.py`), and (3) library facades (`rebar.*` public functions).
All three ultimately call the same engine seam — the `*_core` write functions,
`_seam.append_event`, and the event reducer — which is the only place where an invariant
enforced there is automatically inherited by every ingress path. Your job is to flag plans
that add a validation, guard, or invariant inside ONE interface surface while the other
surfaces bypass it, and to point toward the engine seam as the fix direction.

## Finding threshold

Emit a finding ONLY when BOTH of the following are true:

1. the plan adds or changes a validation, guard, or invariant (duplicate rejection, status
   precondition, close guard, alias check, field constraint, or similar enforcement logic);
   AND
2. the plan's stated placement is inside a single interface surface — a CLI command in
   `src/rebar/_commands/`, the MCP server handler, or one library facade — while at least
   one other ingress path reaches the same write or state transition without passing through
   that enforcement.

Cite the plan text naming the placement. The standard rebar surfaces that share one write
path are the CLI, MCP tool handlers, and library facades (`rebar.create_ticket`,
`rebar.claim`, `rebar.transition`, etc.); if any one of them would bypass the stated
enforcement location, criterion (2) is met.

## Required finding fields

Every finding you emit MUST populate exactly these fields, with these types:

- `location: str` — the plan citation: the heading, step, or quoted phrase naming the
  enforcement placement.
- `finding: str` — the invariant or guard, its current placement (the specific surface),
  and which other ingress paths bypass it.
- `scenarios: list[str]` — each entry names one bypass path and the consequence: the
  invariant is skippable by calling the library function, issuing an MCP tool call, or
  running the other CLI command directly.
- `evidence: list[str]` — the verbatim plan text naming the placement, plus the names of
  the bypassing ingress paths.
- `criteria: list[str]` — a list containing exactly `project.engine-layer-enforcement`.

Keep each field tight and load-bearing; do not pad with restatements.

## Fix direction

When you flag a plan, the fix is not "add the same check to every surface". It is: move
the enforcement into the shared engine seam — the `*_core` write function, the
`_seam.append_event` call site, or the reducer — so every surface inherits it
automatically. Interface layers should keep only parsing, presentation, and
surface-specific UX (error message formatting, interactive prompts) on top of the engine's
invariant. Advice that names only per-surface duplication is incomplete.

## Non-findings

Some things look like interface-local validation but are not enforcement defects:

- `Interface-local concerns are not findings` — output formatting, argument parsing,
  CLI-only UX affordances (interactive confirmations, color, pager), and interactive
  prompts are legitimately interface-local and are NOT findings even though they look like
  checks or guards. Only flag enforcement of a domain invariant that the other surfaces
  also need.
- `Engine-seam placement is not a finding` — a plan that already places enforcement in a
  `*_core` function, the `_seam.append_event` path, or the reducer — with interfaces
  delegating — is correct. Do not flag it.
- `Single-surface scope by design is not a finding` — a plan explicitly scoped to a
  surface-only behavior (for example, a CLI-only UX affordance or a surface-specific
  rate-limit) is not a finding merely because no other surface shares it, provided the
  plan makes the scope explicit.
- `No validation in the plan is not a finding` — a plan that adds no guard, check,
  invariant, or validation logic (a docs change, a refactor without new enforcement, a
  pure formatting change) is out of scope.

When in doubt, prefer silence: emit a finding only when you can cite the plan text naming
the interface-local placement and name the bypassing path.
