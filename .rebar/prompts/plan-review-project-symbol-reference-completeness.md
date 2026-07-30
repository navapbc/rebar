---
schema_version: 1
title: Symbol reference completeness
description: Find plans whose affected-file/call-site inventory was hand-enumerated with grep or is demonstrably incomplete rather than derived from the symbol graph.
execution_mode: single_turn
category: plan-review-criterion
dimension: project-invariants
---
You are reviewing a rebar plan for **symbol reference completeness**. This checkout has
the Serena MCP server configured (LSP-backed, via Pyright over `src/rebar`) for semantic
code navigation. When a plan proposes changing, renaming, or removing a symbol, its
affected-file / call-site / patch-site inventory must be derived from Serena's symbol
tools (`find_referencing_symbols`, `find_symbol`) rather than hand-enumerated with `grep`
or eyeballing — hand enumeration reliably misses call sites, and an incomplete Scope
silently under-delivers the change. Your job is to flag inventories you can show are
incomplete or were plainly not derived from the symbol graph — not to speculate about
thoroughness you cannot check.

## Finding threshold

Emit a finding ONLY when both of the following are true:

1. the plan names, changes, renames, or removes a specific symbol (a function, method,
   class, or module-level name) and enumerates an affected-file / call-site / patch-site
   list for it; AND
2. you can show that enumeration is incomplete or was not symbol-graph-derived — either
   because you can name a concrete call site, reference, or affected file the plan omits
   (verified via `find_referencing_symbols` or an equivalent grep sweep you performed), or
   because the plan's own text states the inventory was produced by hand/`grep`/reading
   rather than by symbol-reference lookup.

The two branches of (2) are INDEPENDENTLY sufficient, and they have different evidence bars:

- **Omission branch** — you name a concrete call site, reference, or affected file the plan
  misses. You have code access: use it. Look the symbol up, and grep for its name as a string
  before concluding the inventory is complete.
- **Stated-method branch** — the plan's own text says the inventory came from `grep`, from
  reading, or from memory (for example "I went through the tree with grep and these are the
  files"). That is admissible **on its own**, with the plan's sentence as the evidence and
  **without** naming an omitted site: the defect being flagged is an ungrounded derivation
  method whose known failure mode is silent omission. Say so plainly rather than abstaining
  because you could not also produce a missing site.

What is NOT a finding either way: a bare suspicion that the list "might be incomplete".

## Required finding fields

Every finding you emit MUST populate exactly these fields, with these types:

- `location: str` — the plan citation: the heading, step, or quoted phrase enumerating
  the affected files/symbols/call sites.
- `finding: str` — the specific symbol whose reference inventory is incomplete or
  ungrounded, plus the concrete evidence (a named omitted site, or the plan's own
  hand-enumeration language).
- `scenarios: list[str]` — each entry names one concrete file, call site, or reference
  that the plan's inventory omits (or would omit if the missing derivation step is not
  performed), and the consequence (an unpatched call site, a broken caller, a missed
  test file).
- `evidence: list[str]` — the verbatim plan text enumerating the inventory, together with
  the specific omitted site(s) or the textual signal of hand enumeration.
- `criteria: list[str]` — a list containing exactly `project.symbol-reference-completeness`.

Keep each field tight and load-bearing; do not pad with restatements.

## Re-derivation guidance

When you flag a plan, the fix is not "double-check the list" in the abstract. It is:
run `find_referencing_symbols` on each symbol the plan changes to get every structural
reference, AND separately grep the codebase for that symbol's name **as a string** (for
example inside `monkeypatch.setattr(...)`, `getattr(obj, "name")`, `importlib.import_module`,
dynamic dispatch tables, or string-based test parametrization) — the LSP resolves only
structural references and will not surface these. Advice that names only one of the two
is half-right and should not be treated as sufficient re-derivation.

## Non-findings

Some things look like incompleteness but are not. Do NOT emit a finding for these:

- `Silence about method is not a finding` — a plan that says nothing at all about HOW its
  inventory was produced is not a finding on that basis; flag it only if you can show the
  inventory is missing something. This covers SILENCE only. It does not override the
  stated-method branch above: a plan that positively says it grepped, read, or remembered its
  way to the inventory IS a finding on that statement alone, even when you cannot find a
  missing site — the derivation method is the defect there, and a complete result from an
  unsound method is luck, not evidence. Say that plainly and keep it advisory.
- `No inventory at all is not a finding` — a plan with no file/symbol/call-site inventory
  to evaluate (a pure docs, research, or exploratory ticket that names no symbol to
  change) is out of scope; there is nothing to check for completeness.
- `A plan explicitly scoped to one file or one symbol with no other references` is not a
  finding merely for being narrow — narrowness is only a defect when you can name a real
  reference it misses.

When in doubt, prefer silence: emit a finding only when you can name the concrete missing
or ungrounded site.
