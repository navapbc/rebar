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
allowed set differs (a *profile*): `show`/`list`/`search`/`session-logs` default
to `json` and add `llm`; `ready` is `text`(ids)/`llm`/`json`; everything else is
`text`/`json`.

All of this lives in **one** place — `src/rebar/_engine_support/output.py`
(`parse_output(...)`); every command calls it **in-process**, so the parsing,
validation, and error text are never duplicated across commands.

## Per-command output contract

Always-JSON reads (no flag needed): `show`, `list`, `search`, `session-logs`,
`deps`, `list-descendants`, `get-file-impact`, `get-verify-commands`,
`clarity-check`.

Structured via `--output json`:

| command(s)                       | schema                    |
|----------------------------------|---------------------------|
| `show` / `list` / `search` / `ready` / `session-logs` | `ticket_state` |
| `show`/`list`/`ready`/`session-logs` `--output llm` | `ticket_state_llm` |
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
| `fsck`                           | `fsck`                    |
| `review` (CLI/library)           | `review_result`           |
| `verify-completion` (CLI/library) | `completion_verdict`     |
| `grounding-info`                 | `grounding_info`          |
| `review-plan`                    | `plan_review_verdict`     |
| `review-code-gate`               | `code_review_verdict`     |
| `sign`                           | `sign_result`             |
| `verify-signature`               | `verify_signature_result` |
| `verify-signature` (not found)   | `error_envelope`          |
| `get-workflow-status` / `get-workflow-result` | `workflow_run` |
| `export`                         | `export`                  |

The authoritative version of this table is `schemas.OUTPUT_SCHEMAS` in
`src/rebar/schemas/__init__.py` — the registry the coverage guard consumes.

### `creation_channel` in `ticket_state`

`show` / `list` / `search` / `ready` carry an optional **`creation_channel`** field on
every ticket (epic jira-reb-977, story 6fe2): the public interface that produced the
ticket's genesis `CREATE`, from a closed six-value enum
(`common.schema.json#/$defs/creation_channel`):

- **`cli`** — the `rebar` CLI; **`mcp`** — the MCP server's write tools; **`python`** — a
  direct `rebar.*` library call (the default at the library boundary).
- **`jira`** (Jira-inbound) and **`import`** (NDJSON `rebar import`) are now emitted
  (story e622): the reconciler stamps `jira` on the CREATE it writes for an inbound Jira
  issue, and `rebar import` stamps `import` on the fresh local ticket. An imported ticket
  reports `import` even when the exported source record carried a different channel — the
  source's origin is preserved as `source_*`, never copied into `creation_channel`.
- **`unknown`** is a **projection-only fallback**: a legacy ticket whose `CREATE` predates
  the field reduces to `unknown`. It is never a valid live-write value (the write path's
  `validate_creation_channel` rejects it).

The field is **immutable** — stamped once at `CREATE` and never overwritten by an `EDIT`.
A companion **`creation_channel_inferred`** (`{"const": true}`) marks a *heuristically
inferred* channel: a channel-less legacy CREATE bearing the exact legacy-Jira envelope
signature (`jira-` id + `reconciler` author + `reconciler` env_id) reduces to
`creation_channel="jira"` with `creation_channel_inferred=true`; a recorded channel never
carries the marker, and it is otherwise absent.

> **Trust boundary.** `creation_channel_inferred` is heuristic **audit** metadata, **not a
> security attestation**. A recorded channel reflects the real ingress; an inferred `jira`
> is a best-effort backfill for pre-feature history, derived only from the immutable genesis
> envelope (ticket_id / author / env_id). Do not treat it as cryptographic proof of origin —
> use the signed-attestation machinery when origin trust matters.

`creation_channel` is orthogonal to actor/environment provenance (`author`/`env_id` =
who/where) and to `source_*` (where an imported ticket came from): it records **which of
rebar's own interfaces** the create came through, and is present on every ticket rather than
only imported ones. The generated `rebar.types` names it
`TicketState.creation_channel: NotRequired[CreationChannel]`.

## `error_envelope` — the machine-readable failure channel

When a command **fails** while `--output json` is requested, it emits an
`error_envelope` (`{error, input, message[, exit_code]}`, `common.schema.json`) on
**stdout** — so an agent's `json.load` always succeeds and it never has to parse
human stderr prose. The human prose still goes to **stderr**; in text mode the
envelope is suppressed (text-mode stdout is unchanged). The optional `exit_code`
mirrors the process exit status (see [exit-codes.md](exit-codes.md)).

The shared emitter is `output.error_envelope(...)`
(`src/rebar/_engine_support/output.py`); every command's json-mode
failure branch routes through it in-process. Pinned by
`tests/interfaces/contracts/test_error_envelope.py`.

Not every non-zero exit is a "failure" in this sense: the per-ticket **gates**
(`check-ac`/`quality-check`) use exit 1 as a *verdict*; **tolerant reads**
(`summary`/`list-descendants`/`get-file-impact`/`scratch get`) return an empty
result at exit 0; and `clarity-check` has its own always-JSON contract outside the
`--output` system. Those are documented exemptions, not envelope cases.

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

- `tests/interfaces/contracts/test_schema_outputs.py` — drives every shape's REAL engine
  output (library + CLI) and validates it against its schema.
- `tests/interfaces/facades/test_mcp_output_schema_coverage.py` — the **MCP coverage
  guard**: the set of tools under test is sourced mechanically from
  `list_tools()`, every outputSchema-advertising tool is driven on a fixture store
  and its result validated against the canonical schema, and any advertiser
  without a canonical shape must be a documented exemption (so a new MCP tool
  cannot ship an unvalidated `outputSchema`).
- `tests/interfaces/contracts/test_schema_coverage.py` — the **coverage guard**: every
  *output* schema file is wired into `OUTPUT_SCHEMAS` (the `COMMON`, `INPUT_SCHEMAS`,
  and `CONTRACT_SCHEMAS` files are exempted), every registry entry resolves, and
  every command whose `--help` advertises `--output` is covered (so a new
  `--output` command without a schema fails CI).

**MCP outputSchema exemptions (documented in the coverage test):** the write
tools (`comment`/`tag`/`archive`/`edit`/`link`/`set_*`/`compact`/…) and the MCP
`fsck` tool return a generic `{result: <str>}` ack with no canonical shape;
`transition_ticket`/`reopen_ticket` advertise **no** `outputSchema` because their
`{ticket_id, from, to, …}` result uses the Python reserved word `from` (they
return a plain dict; their CLI/library JSON is still pinned to `transition_result`);
`reconcile` has no canonical schema for its plan/result; and `review_ticket`
(`rebar.llm`) returns a plain dict because it makes a **live LLM call** — it must
not be auto-driven on the fixture store in CI, so it advertises no `outputSchema`
and is a documented exemption. Its **CLI/library** `--output json` path *is* pinned
to `review_result` via `OUTPUT_SCHEMAS["review"]` (the model-produced shape is still
normalized + schema-validated before it is returned; see
[llm-framework.md](llm-framework.md)).

Adding a new structured output therefore means: author the schema (reuse
`common` `$ref`s), register it in `OUTPUT_SCHEMAS`, and add a conformance case —
the guard enforces the rest.
