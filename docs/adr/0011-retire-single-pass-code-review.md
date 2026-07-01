# ADR 0011: Retire the single-pass code-review route (one-way door)

- **Status:** Accepted
- **Context:** Epic *Agentic code-review capability* (`b744-4fb9-2d05-4b49` / dowdy-swear-bird),
  story WS4 (`98ff-d3b8-d2f7-4a86` / side-haiku-dew). Builds on ADR 0010 (overlay escalation)
  and `docs/review-kernel.md`.

## Context

The original `code_review.py` was a THROWAWAY single-pass demo: select reviewers by glob →
run them in parallel → `aggregate_findings` → `finalize_findings`. It had NO Pass-2 verifier,
NO deterministic Pass-3 decision, NO Pass-4 coaching — none of the four-pass review kernel's
guarantees. WS1–WS3 built the real four-pass code-review GATE (`gates/code-review.yaml`)
consuming the kernel. Keeping both would be a half-migration: two code-review code paths, one
of which silently lacks verification/decision rigor.

## Decision

**Retire the single-pass route; `review_code` becomes the gate-backed shim.** The public
surface is preserved unchanged — the `rebar.llm.review_code(...)` callable, the MCP
`review_code` tool, and the `rebar review-code` CLI keep their names, signatures, and
`review_result` return shape. What changes is the IMPLEMENTATION behind that surface:

- `single_pass.py` (the moved `code_review.py`) is **deleted**. Its `_review_code_inner` /
  `select_code_reviewers` / `_changed_from_diff` / `_compose_context` are gone (the diff-read
  lives on in `assemble.py`'s self-contained copy; the gate selects overlays itself).
- `review_code` is reimplemented in `shim.py`: when the capability is enabled it runs
  `produce_code_review_verdict` and TRANSLATES the `code_review_verdict` → `review_result`;
  when disabled it returns a valid EMPTY `review_result` + a 'capability disabled' note.
- `code_review/__init__.py` no longer lazily re-exports `single_pass`; it exposes the
  gate-backed `review_code` directly.

## This is a ONE-WAY DOOR (the honest back-out)

The single-pass code is DELETED, so flipping `verify.enable_code_review` back OFF does **not**
restore single-pass behavior — it makes the capability INERT (`review_code()` returns the empty
disabled result, never an error/stub). The flag default (OFF) is the **inertness** control, not
a behavior-restore: it governs whether the gate runs, not which of two implementations runs.
The only way back to the single-pass demo is `git revert` of the retirement — a deliberate code
change, not a config toggle. This supersedes any earlier "reverting the flag restores prior
behavior" phrasing.

## Consequences

- One code-review path, with the full four-pass rigor. No silent half-migration.
- Off-by-default + source-separated: with the flag off, the capability is inert and isolated
  in `rebar.llm.code_review`; nothing runs.
- The `select_code_reviewers` helper leaves the public surface (it was single-pass-only); the
  `review_code` / MCP / CLI surface is unchanged.
- Tests that exercised the single-pass aggregate route are removed; the gate-backed disabled
  and enabled paths are covered by `tests/unit/test_code_review_ws4.py` +
  `tests/interfaces/store/test_llm_framework.py`.
