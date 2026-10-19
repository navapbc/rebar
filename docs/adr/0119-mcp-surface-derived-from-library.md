# ADR 0119 — The MCP tool surface is derived from the library facade (MCP only)

**Status:** Accepted (story `masonic-excusable-guppy` / `c296-4bc7-26be-489d`, epic
`current-cronish-norwayrat`)
**Date:** 2026-10-19
**Relation:** SUPERSEDES the "no generated adapters (Pattern A)" clause of ticket
`topaz-blubbery-mice` (`c996-8a5f-5123-4f8e`) for the MCP surface only. ADR 0100 (the CLI command
and capability registry) is untouched.

## Context

`topaz-blubbery-mice` (2026-08) built the behavioural write-parity oracle and decided not to
generate the library, CLI, or MCP adapters: behaviour is single-sourced in the library, and the
adapters carry hand-written value that a generator would lose (LLM-facing tool descriptions, CLI
ergonomics). The oracle asserts behaviour on three write operations; it cannot see shape.

On 2026-09-02 bug `lowly-lawrencium-ovenbird` (`3a02-66ea-9229-470c`) found 41 canonical properties
missing from the MCP output mirror and model defaults emitting schema-violating nulls, while the
name-only parity gate (`scripts/check_mcp_library_parity.py`, a 919-line manifest and a 486-line
test) had passed throughout. The 48 library-backed MCP tools were closures that each re-declared
one `rebar.*` signature by hand, and three undeclared input divergences existed
(`list_tickets.priority`, `bridge_run.profile`, the `transition_ticket` parameter order).

## Decision

For the MCP surface only:

1. **Output.** `outputSchema`s are the canonical `src/rebar/schemas/*.json` files, inlined and
   advertised directly; tools return the library payload unchanged, absent keys stay absent, and
   the pydantic mirror is deleted (story `quilted-nepotic-kitty`).
2. **Input.** The 48 library-backed tools are derived at registration from the annotated library
   signatures through one factory (`rebar._mcp_derive.derive_tool`) plus an explicit override
   table (`rebar._mcp_tool_specs.TOOL_SPECS`). The table carries every deliberate divergence
   (`strip`, `params`, `order`, `pins`, each with its reason in `divergence`), every pinned
   keyword argument, every per-operation hook (read-only and Jira-sync gates, the cross-session
   advisory, delivery status, write acks, list budgets), and each LLM-facing description
   verbatim. The 16 MCP-only tools stay hand-written in their registrars.

The CLI stays under ADR 0100; Pattern A remains rejected there, because CLI ergonomics are not a
projection of the library signature.

## Consequences

- One contract per shape. A schema edit changes the wire without a second edit, and a library
  parameter change reaches the MCP input schema automatically.
- Divergence is declared in the override table or it does not exist. The name-only parity gate,
  its manifest, and its test are deleted; the model drift gate went with the mirror.
- What a client sees is pinned byte-for-byte by a golden of the live tool surface
  (`tests/interfaces/facades/mcp_tool_surface_golden.json`: `inputSchema`, `description`,
  `annotations` for all 64 tools). A library change that alters an MCP input schema therefore
  fails that test until the golden diff is reviewed and committed, which is the point. The
  golden was captured on Python 3.12, the version the deployed server image and
  `.github/python-version.txt` use, and the comparison is byte-for-byte on 3.11 and 3.12.
  Python 3.13+ strips docstring indentation at compile time (gh-81283), so there the test
  compares descriptions after `inspect.cleandoc`. The derived tools' descriptions are string
  literals, not docstrings, so on 3.13+ their continuation lines keep the 3.12 indentation that
  the replaced closures' docstrings lost: a whitespace-only difference.
- Explicit nulls the mirror invented disappeared (pre-1.0 BREAKING, recorded in
  `docs/release-notes.md`).
- The hand-written value the original clause protected — LLM-facing text and deliberate MCP
  shapes — is preserved in the table instead of in code copies.
