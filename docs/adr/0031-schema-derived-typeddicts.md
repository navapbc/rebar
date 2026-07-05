# ADR 0031 — Schema-derived TypedDicts as the public `rebar.*` return contract

**Status:** Accepted (epic sail-shoe-smear — OSS-readiness / story 3a10 — irate-draw-ulcer)
**Date:** 2026-07-04

## Context

The public `rebar.*` library facade returns plain `dict`/`list` shapes with no named,
documented return contract. Published-library consumers cannot tell which keys are
guaranteed, and get no type-checker support (`d["status"]` is untyped `Any`). The
OSS-readiness review flagged this as the main library-API gap.

Two facts shape the solution:

- **The canonical contract already exists.** rebar's structured outputs are already
  described by the JSON Schemas in `src/rebar/schemas/*.schema.json`, wired through
  `rebar.schemas.OUTPUT_SCHEMAS` and advertised to MCP clients as `outputSchema`s.
  A second, hand-authored source of return types would immediately drift from them.
- **Pydantic is deliberately not a core dependency.** rebar's core runtime deps are
  `pyyaml`/`jsonschema`/`referencing`; adding Pydantic (or any runtime-validating
  model layer) to the base facade is out of scope. Consumers must get types with
  **zero runtime change** — returns stay plain dicts.

The schemas also have a well-worn precedent for keeping a **derived artifact** honest:
the prompt-index drift gate (regenerate-in-place, then `git diff --exit-code`).

## Decision

Give the schema-backed subset of the facade a typed return contract via
`TypedDict`s **generated from the canonical JSON Schemas**, static-typing only.

### 1. `TypedDict`, generated, not Pydantic and not hand-authored

`src/rebar/schemas/gen_types.py` reads the canonical schemas and writes
`src/rebar/types.py` (`python -m rebar.schemas.gen_types`). The facade's return
annotations reference these types under `TYPE_CHECKING` (with
`from __future__ import annotations`, so they cost nothing at runtime and add no
import cycle). No runtime return value changes; existing callers are untouched.

A CI **drift gate** (in `.github/workflows/test.yml`, mirroring the prompt-index
gate) regenerates `types.py` and fails on a stale committed file — the schemas stay
the single source of truth.

### 2. Open schemas → closed TypedDicts of the *documented* contract

Every public output schema is `additionalProperties: true` (the event-sourced shape
may grow without breaking consumers). `TypedDict` cannot express arbitrary extra
keys, so we emit **closed** TypedDicts that name the *guaranteed/known* keys:
schema-`required` keys are normal fields; non-required keys are `NotRequired[...]`.
This keeps the "required fields are present" guarantee (unlike a blanket
`total=False`) while marking optional keys. The runtime dict stays open; reading a
key not named in the TypedDict is outside the typed contract **by design**. Where the
library legitimately adds keys beyond the base schema (e.g. `clarity_check` adds
`passed`), the facade `cast`s to the documented contract at the boundary.

### 3. A small custom generator, not an off-the-shelf tool

The generator resolves exactly the constructs rebar's schemas use — cross-file
`$ref` into `common.schema.json#/$defs/*`, `["T","null"]` unions, enum `$ref` →
`Literal[...]`, arrays, and the reserved-word key `from` (functional `TypedDict(...)`
form). Off-the-shelf generators (`datamodel-code-generator`, `jsonschema-gentypes`)
have only partial draft-2020-12 / cross-file-`$ref` support and would add a
dependency; a focused ~250-LOC generator is simpler and dependency-free, and mirrors
the existing in-repo `regenerate_prompt_index` precedent. Its output is canonicalized
through `ruff` so the drift gate is stable.

### 4. Literal only from a formal `enum`

A value vocabulary that lives only in a schema `description` string (e.g.
`workflow_run.status`) maps to `str`, not `Literal` — we do not over-promise a
Literal union the schema does not formally constrain.

### 5. Scope = the schema-backed subset

We generate + annotate only the facade functions whose return shape has a canonical
`OUTPUT_SCHEMAS` entry. Functions with no canonical output schema (`append_session_log`,
`start_session_log`, `attach_commits`, `export_tickets`, `import_tickets`,
`reconcile`, and `run_workflow`'s distinct result shape) stay `dict[str, Any]` and
are explicitly out of the generated set.

## Consequences

- Consumers get named keys + type-checker support (`from rebar.types import TicketState`)
  with zero runtime cost and no new dependency.
- The schemas remain the single source of truth; the drift gate prevents silent skew.
- The typed contract is intentionally a **floor** (documented keys), not a closed
  universe — extra runtime keys are allowed and untyped, matching the schemas'
  `additionalProperties: true`.
- `create_ticket` is `@overload`-typed on `return_alias` so `str`-returning callers
  keep `str` and `return_alias=True` callers get `CreateResult`.
