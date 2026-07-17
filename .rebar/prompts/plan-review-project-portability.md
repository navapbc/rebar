---
schema_version: 1
title: Rebar portability
description: Find concrete rebar portability failures across supported client shapes.
execution_mode: single_turn
category: plan-review-criterion
dimension: project-invariants
---
You are reviewing a rebar plan for **portability** across the full set of supported
client shapes. rebar is a harness that must run identically for every consumer — as a
Python library, a CLI, and a remote MCP server — against many kinds of target project,
on many platforms and venues, and from many project locations. A plan violates
portability when it silently bakes in an assumption that is only true for *one* corner
of that support matrix and would break another. Your job is to flag those assumptions
with a concrete, falsifiable counterexample — not to speculate.

## Finding threshold

Emit a finding ONLY when you can construct a complete counterexample: all four of the
following elements must be present, named, and causally connected. If any one is
missing, do not emit the finding — silence is correct when the assumption is not
demonstrably breaking. A finding requires:

1. a `cited plan mechanism` — the specific step, command, path, or design decision in
   the plan that carries the assumption;
2. a `materially different supported client shape` — a real cell of the support matrix
   below that differs from the one the plan assumes, not a hypothetical or unsupported
   configuration;
3. a `causal failure mechanism` — the concrete reason the cited mechanism cannot work
   in that shape (a missing binary, an unavailable filesystem, an OS path rule, an
   absent dependency), stated as cause and effect;
4. an `observable breakage scenario` — a specific, observable outcome (an error, a
   wrong result, a crash, a no-op) that a user in that shape would actually witness.

All four must appear together. A plan mechanism plus a vague "might not be portable" is
not a finding; you must carry it through to an observable breakage scenario in a named
alternate shape.

## Required finding fields

Every finding you emit MUST populate exactly these fields, with these types:

- `location: str` — the plan citation: the heading, step number, or quoted phrase in
  the plan where the assumption lives.
- `finding: str` — the assumption plus its causal mechanism: state what the plan
  assumes and precisely why that assumption fails.
- `scenarios: list[str]` — the alternate client shape plus the observable breakage: each
  entry names a matrix cell that differs and the concrete outcome a user there would see.
- `evidence: list[str]` — the plan quote plus grounding facts: the verbatim text from
  the plan that carries the assumption, together with any codebase or platform facts you
  relied on.
- `criteria: list[str]` — a list containing `project.portability`.

Keep each field tight and load-bearing; do not pad with restatements.

## Supported client-shape matrix

These are the shapes rebar must support. A finding's alternate shape MUST come from a
cell of this matrix — anything outside it is out of scope and not a finding.

- `Harness`: Python library, CLI, remote MCP; no Claude Code or Codex dependency.
- `Target project`: Ruby, Python, Java, Next.js, .NET, Terraform subprojects in a monorepo.
- `Platform and venue`: macOS, Windows, Linux, BSD, CI, servers, developer workstations.
- `Project location and access`: in-checkout current working directory, explicitly located workspace, server outside the checkout, no unrestricted-local-filesystem assumption.

When you assert that a plan step breaks in an alternate shape, name the specific cell —
e.g. that a step assuming a POSIX shell breaks the `Windows` platform, or that a step
shelling out to `python` breaks a `Ruby` or `.NET` target project, or that a step
reading a hardcoded `.rebar/` path under the current working directory breaks a
`server outside the checkout` deployment.

## Non-findings

Some things look like portability problems but are not. Do NOT emit a finding for these:

- `Silence about portability is not a finding` — a plan that simply does not discuss
  portability, but whose mechanisms are shape-agnostic, is fine; absence of a portability
  section is never itself a violation.
- `Project-specific behavior behind project configuration or an explicit extension boundary is allowed` — behavior that is intentionally gated on project config, a documented
  extension point, or an explicit capability boundary is portable by design; a shape that
  simply does not opt into that configured behavior is not "broken" by it.

When in doubt, prefer silence: emit a finding only when the four-element counterexample
above is fully and concretely satisfiable.
