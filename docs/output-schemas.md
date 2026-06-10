# Output schemas — rebar's machine-readable output contract

rebar emits structured output through **one** canonical flag and documents every
distinct JSON shape with a **JSON Schema** that is validated across all three
interfaces (CLI, library, MCP) in CI.

## The `--output` / `-o` flag

`--output VALUE` (short alias `-o VALUE`) is the single structured-output
selector. The legacy `--json` and `--format=*` flags were removed (pre-1.0).

| value  | meaning                                                                   |
|--------|---------------------------------------------------------------------------|
| `text` | human/default rendering (id-list for `ready`, human report for the rest)  |
| `json` | the structured machine shape (documented by a schema below)               |
| `llm`  | token-minified short-key NDJSON (`show`/`list`/`ready` only)              |

Accepted spellings: `-o json`, `--output json`, `--output=json`. Per-command the
allowed set differs (a *profile*): `show`/`list`/`search` default to `json` and
add `llm`; `ready` is `text`(ids)/`llm`/`json`; everything else is `text`/`json`.

All of this lives in **one** place — `src/rebar/_engine/ticket_output.py`
(`parse_output(argv, profile)`); the bash commands shell into it, so the parsing,
validation, and error text are never duplicated between bash and Python.

## Per-command output contract

Always-JSON reads (no flag needed): `show`, `list`, `search`, `deps`,
`list-descendants`, `get-file-impact`, `get-verify-commands`, `clarity-check`.

Structured via `--output json`:

| command(s)                       | schema                    |
|----------------------------------|---------------------------|
| `show` / `list` / `search` / `ready` | `ticket_state`        |
| `show`/`list`/`ready` `--output llm` | `ticket_state_llm`    |
| `deps`                           | `deps_graph`              |
| `next-batch`                     | `next_batch`              |
| `list-descendants`               | `list_descendants`        |
| `clarity-check`                  | `clarity_result`          |
| `check-ac` / `quality-check`     | `gate_result`             |
| `validate`                       | `validate_report`         |
| `bridge-status`                  | `bridge_status`           |
| `get-file-impact`                | `file_impact`             |
| `get-verify-commands`            | `verify_commands`         |
| `scratch get/set/clear`          | `scratch_envelope`        |
| `show` (not found)               | `error_envelope`          |
| `bridge-fsck`                    | `bridge_fsck`             |
| `create`                         | `create_result`           |
| `claim`                          | `claim_result`            |
| `transition` / `reopen`          | `transition_result`       |
| `delete`                         | `delete_result`           |
| `summary`                        | `summary`                 |
| `list-epics`                     | `list_epics`              |
| `fsck`                           | `fsck`                    |

The authoritative version of this table is `schemas.OUTPUT_SCHEMAS` in
`src/rebar/schemas/__init__.py` — the registry the coverage guard consumes.

## Source of truth & drift guard

**The hand-authored JSON Schema files under `src/rebar/schemas/*.schema.json` are
the single source of truth** for output shapes. Shared sub-objects (a comment, a
dep, a `{path,reason}` entry, the status/type enums, …) are authored once in
`common.schema.json` and `$ref`'d everywhere, so e.g. `get-file-impact` and
`TicketState.file_impact[]` can never drift. Because those are cross-file refs,
validate with `rebar.schemas.validator(name)` (it wires a `referencing` registry)
rather than calling `jsonschema.validate(instance, load(name))` directly.

The MCP server's pydantic models (`src/rebar/mcp_server.py`, e.g. `TicketStateOut`)
**mirror** these schemas to advertise an `outputSchema` to MCP clients; they are
not a second source of truth. Tests pin both:

- `tests/interfaces/test_schema_outputs.py` — drives every shape's REAL engine
  output (library + CLI) and validates it against its schema; asserts the typed
  MCP read tools advertise an `outputSchema`.
- `tests/interfaces/test_schema_coverage.py` — the **coverage guard**: every
  schema file is wired into `OUTPUT_SCHEMAS`, every registry entry resolves, and
  every command whose `--help` advertises `--output` is covered (so a new
  `--output` command without a schema fails CI).

Adding a new structured output therefore means: author the schema (reuse
`common` `$ref`s), register it in `OUTPUT_SCHEMAS`, and add a conformance case —
the guard enforces the rest.
