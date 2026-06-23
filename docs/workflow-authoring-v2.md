# Workflow authoring v2: contract-bearing prompts & steps

> **Audience: rebar developers and workflow *authors*.** Like the visual editor, this
> is edit-time authoring. rebar *clients* (agents/humans driving tickets over the
> CLI/MCP) run workflows; they don't author prompts or step contracts and never need
> any of this. Nothing here is on the client/runtime path.

This document is the single reference for the **contract-bearing prompt/step model**
that powers the typed palette, the in-editor inspector, the prompt library/authoring,
and the workflow validator. The companion [workflow-editor.md](workflow-editor.md)
covers the visual editor itself (`rebar workflow edit`); this page covers the *model*
the editor presents.

The unifying requirement: **every workflow step kind advertises an INPUT contract, an
OUTPUT contract, and a description**, so the editor can present a typed palette and
validate edits, and the runtime can validate produced values without inventing
protocol.

---

## 1. Prompts: the decomposed-triple model

A prompt is a Markdown file with YAML **front-matter**. Reviewers are now a *subset* of
prompts (see §4) — there is no separate reviewer catalog.

### 1.1 The closed front-matter key set

Front-matter carries each prompt's contract. The key set is **closed** — authored in
`rebar.llm.prompts.FRONT_MATTER_KEYS`, in canonical emit order:

| Key              | Meaning |
|------------------|---------|
| `schema_version` | Front-matter format version (stamped by the writer; see §1.3). |
| `title`          | Human label for the palette. |
| `description`    | What the prompt does (shown in the inspector/palette). |
| `inputs`         | The prompt's INPUT contract — a **named registry schema** (see §3). |
| `outputs`        | The prompt's OUTPUT contract — a **named registry schema** (see §3). |
| `execution_mode` | `single_turn` \| `agentic` (see the ADR, §6). Fixed on the prompt. |
| `category`       | Advisory palette grouping (`review`/`verifier`/`transform`/`code`/`exploration`). The **explicit** `category: review` flag is what makes a prompt a reviewer — never inferred from the output schema. |
| `model`          | Optional model override. |
| `tags`           | Free-form labels. |
| `dimension`      | A reviewer's review dimension. |
| `applies_to`     | `fnmatch` globs for rule-based reviewer selection. |
| `langfuse_prompt`| Langfuse prompt name (read-replica only; never consulted for text). |
| `default`        | Whether this reviewer is in the default set (exactly one default; see §4). |

Heavy JSON Schemas are referenced **by name** (`inputs: my_input`), never inlined.

Unknown keys are **WARN+PRESERVED** by the writer (appended deterministically, sorted),
so a newer key written by a newer binary survives a round-trip on an older one.

### 1.2 The `.rebar/prompts/<id>.md` override mechanism

Prompt text resolution order (`rebar.llm.prompts.get_prompt`):

1. **Project override** — `<repo>/.rebar/prompts/<id>.md`, if present.
2. **Built-in packaged** — `src/rebar/llm/reviewers/<id>.md` (shipped in the wheel).

An override fully replaces the built-in for that id. An override that changes a
prompt's `outputs` contract is **breaking** and is flagged by the linter
(`prompt_override_drift`, surfaced through `rebar workflow validate`).

### 1.3 The canonical writer & read-side version coexistence

`write_front_matter(meta, body)` is the **parse-split-rejoin canonical writer**:

- known keys in canonical order, unknown keys WARN+PRESERVED (sorted, appended);
- `schema_version` stamped; LF line endings with a single trailing newline on the
  front-matter block;
- the body preserved **byte-for-byte** (no-trailing-newline, embedded CRLF, and a body
  that itself starts with `---` all survive);
- **idempotent**: `write(*split_raw(write(m, b))) == write(m, b)`;
- a leading BOM is **refused** (canonical files are BOM-free).

`.gitattributes` pins prompt/index/workflow globs to `eol=lf` so a Windows checkout or
`core.autocrlf` can't rewrite these (which would defeat the `\n`-anchored fence and
change the content hash).

**Read-side version coexistence.** `parse_front_matter` **refuses** a prompt whose
`schema_version` is higher than the running binary understands
(`PromptVersionError`) — it never renders unknown front-matter into the body. The
**write-side** policy: a writer bumps `schema_version` only when it begins emitting a
key a prior version lacked. The documented deploy order is **readers before writers**
(the read-side refuse is the safety net), the same discipline as `TAG_DELTA`.

### 1.4 In-editor authoring & write-back location detection

The editor's prompt library lists built-in + project prompts; you can **create** a new
prompt and **edit** prompt text in place. On Save, the write-back target is
**auto-detected** (`prompt_write_target`) and shown before you save:

- a writable **`nava-rebar` source checkout** (`pyproject.toml` `[project].name ==
  "nava-rebar"` and a writable `src/rebar/llm/reviewers/`) → edit the packaged `.md`;
- else a writable **project override** dir → write `.rebar/prompts/<id>.md`;
- else **refuse** with a clear reason (neither location writable).

Writes are **atomic** (write-temp + `os.replace`) through the canonical writer, and
creating/editing a *packaged* prompt **regenerates the derived index** (§5). Non-happy
paths each surface a clear error, never silent corruption: an invalid/empty id, an id
**collision** on create-new (refuse unless `overwrite`), and the neither-writable case.

---

## 2. Steps: scripted ops are contract-bearing too

Each built-in scripted op declares its contract via `@register_step`:

```python
@register_step(
    "fetch_ticket",
    input_schema="fetch_ticket_input",
    output_schema="fetch_ticket_output",
    description="Fetch a ticket's compiled state. …",
)
def fetch_ticket(ctx): ...
```

`contract_for(name)` returns the `StepContract {input_schema, output_schema,
description}`. A registry-coverage test asserts **every** registered op carries a
contract. All eight built-ins (`fetch_ticket`, `fetch_commits`, `fetch_epic_graph`,
`render_context`, `gate`, `comment_verdict`, `tag`, `set_fields`) are annotated.

The engine seeds a small **injected `${{ inputs.* }}` namespace** —
`ENGINE_INJECTED_INPUTS = {ticket_id, ticket_context, repo_path}` — valid to reference
even when not declared in a workflow's `inputs:` block. That set is the single source
of truth (the linter allow-lists exactly it).

---

## 3. Contract conventions (prompts ↔ ops ↔ registry)

Both surfaces resolve to **named schemas** in the registry
(`rebar.schemas`), so the editor inspector and the validator share one vocabulary:

| Surface | INPUT contract | OUTPUT contract |
|---------|----------------|-----------------|
| **prompt** | front-matter `inputs:` (schema name) | front-matter `outputs:` (schema name) |
| **scripted op** | `@register_step(input_schema=…)` | `@register_step(output_schema=…)` |

The per-step I/O contract schemas live in `src/rebar/schemas/*_input.schema.json` /
`*_output.schema.json` and are collected in `schemas.CONTRACT_SCHEMAS`. They are
consumed directly (by the inspector + linter), not advertised as a command's `--output`,
so the schema coverage-guard exempts them (alongside the workflow DSL input schemas).

The editor inspector surfaces a selected node's **CONSUMES** (input fields), **PRODUCES**
(output fields), and **description**, read-only, with a defined empty state for a node
with no declared contract.

---

## 4. The derived prompt index

`src/rebar/llm/reviewers/index.json` is **generated and committed** — the old
`catalog.json` is gone (fully derived). `build_prompt_index` scans the packaged prompt
front-matter; `regenerate_prompt_index` writes the file. Invariants (raised on
violation): **exactly one `default`** across reviewers, and **no `dimension`
collision**. A removed prompt is **retired** from the index on regeneration;
`langfuse_prompt`/`fallback_file`/`applies_to` are preserved.

### CI drift gate

`.github/workflows/test.yml` runs a **Prompt-index drift gate**: it regenerates the
index and `git diff --exit-code -- src/rebar/llm/reviewers/index.json`. A stale index
(front-matter changed without regenerating) fails the build. Regenerate with:

```bash
python -m rebar.llm.prompts regenerate-index
```

---

## 5. Validation: three-valued, shallow-static + runtime-against-consumer

rebar deliberately does **not** ship a schema-subsumption engine (near-undecidable, no
good library). Instead it pairs a cheap shallow static check with the real runtime net.

### 5.1 Shallow static check (3-state)

`shallow_contract_check(source, target) -> "OK" | "UNKNOWN" | "ERROR"` compares a
producer's OUTPUT schema (`source`) to a consumer's INPUT schema (`target`):

- **ERROR** — a `target` required field is absent from `source`, or a shared field's
  primitive `type` is incompatible (kind-mismatch).
- **UNKNOWN (abstain)** — either schema (or a field) uses `oneOf`/`anyOf`/`allOf`/`not`
  or a `$ref`: no subsumption is attempted, so it abstains rather than guess.
- **OK** — every required target field is present with a compatible/unknown kind.

### 5.2 Runtime validation against the consumer's input contract

At run time the interpreter validates each step's **resolved `with` inputs** against the
**consumer's declared input contract** (op `input_schema` / prompt `inputs`) via the
registry validator — the real safety net:

- a genuine **mismatch** fails the step (`input contract violation (<schema>)`);
- a **validator failure** — the validator itself errors (unresolvable `$ref`, unknown
  schema, any non-`ValidationError`) — surfaces a **distinct** signal
  (`input validation UNAVAILABLE/errored (<schema>)`) and **never silently passes** the
  value (fail-loud);
- a step with **no declared contract** is UNKNOWN → validation is skipped (never failed),
  so contract-less workflows keep working.

The editor exposes the same model live: a debounced **`/validate`** endpoint returns the
defined shape `{ok, errors:[{path, message}], unavailable}`; the panel shows red errors
before Save, a distinct **"validation unavailable"** state on endpoint/validator failure,
and **fail-closed Save** (Save is blocked while errors exist *or* while unavailable). An
**"⚠ unchecked (opaque source)"** badge marks UNKNOWN/opaque sources in the inspector.

---

## 6. `execution_mode`: single_turn vs agentic

`execution_mode` is a **closed `{single_turn, agentic}` enum on the prompt** (defaulting
to `agentic` when absent), **distinct** from the step-level `mode`
(`findings`/`structured`/`text`, which controls output shaping):

- **`agentic`** — the pydantic_ai tool-using path (filesystem + rebar tools, bounded
  tool loop). The default.
- **`single_turn`** — exactly **one** model call with **no tools**, returning structured
  output validated against the prompt's `outputs` contract (a single_turn prompt must
  declare `outputs`).

The decision to fix `execution_mode` on the prompt (rather than a swappable per-call
strategy) is recorded in
[adr/0001-execution-mode-fixed-on-prompt.md](adr/0001-execution-mode-fixed-on-prompt.md).

---

## 7. The editor authoring experience (pointers)

The visual editor ([workflow-editor.md](workflow-editor.md)) composes these pieces:

- a **typed palette** that inserts a scripted op (`uses:`) or a prompt (`prompt:`),
  grouped by the closed category vocabulary;
- the **prompt library** + in-UI create/edit with auto-detected, atomic write-back (§1.4);
- **structured per-field** properties driven by each step kind's contract (with a raw
  "Advanced (JSON)" fallback for uncommon configs);
- the read-only **contract inspector** (§3) and the live **validator** (§5).
