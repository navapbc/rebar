# ADR 0007: Editing a prompt's CONTRACT from the visual editor

- **Status:** Proposed (design spike — recommendation; implementation deferred to the
  follow-on stories in §Consequences)
- **Context:** Workflow authoring v2 (epic *Workflow authoring v2: contract-bearing
  prompt/step model + editor authoring*, `da27`), story `5b9e-2b65`. Discovered from the
  in-editor authoring work shipped by story **6592** (*Editor prompt library +
  authoring/write-back*).

## Context

Editing a prompt's **TEXT** from the visual editor is **already delivered** (story 6592,
closed under epic `da27`). The editor's prompt library lists built-in + project prompts,
creates new prompts, and edits prompt bodies in place with auto-detected, atomic
write-back and derived-index regeneration — see `prompt_authoring.py` (`list_prompts`,
`prompt_write_target`, `save_prompt`) and the `/prompts`, `/prompt`, `/prompt/save`
endpoints in `editor.py`, documented in [workflow-authoring-v2.md §1.4](../workflow-authoring-v2.md).

What is **still read-only** is a step's **CONTRACT**:

- A **prompt's** input/output contract is the front-matter `inputs:` / `outputs:` keys —
  each a **named registry schema** (`inputs: reviewer_input`), resolved through
  `rebar.schemas` (see [workflow-authoring-v2.md §3](../workflow-authoring-v2.md)). Today
  the editor *surfaces* these read-only via `editor_contracts.prompt_contract_view`
  (CONSUMES / PRODUCES / description), but the prompt-save form in 6592 does **not** let
  you change which schema a prompt declares.
- A **scripted op's** contract is declared in Python at import time:
  `@register_step("fetch_ticket", input_schema="fetch_ticket_input",
  output_schema="fetch_ticket_output", …)` populates the in-process `STEP_CONTRACTS`
  dict; `contract_for(name)` reads it back. There is **no file artifact** to edit.

This ADR answers two coupled questions and records a recommendation, scoped to the
**schema-NAME** model (see the explicit fork in §Scope) and to a single persona.

### Persona (who edits contracts, and why it is load-bearing)

The contract editor's actor is the **pipeline developer / prompt author maintaining
workflow steps** — the same technically-fluent persona who already creates and edits
prompt bodies via 6592 and reads the contract inspector. It is **not** a non-technical
end-user. This matters because the right guard-rails differ by persona: a developer can
be shown a registry schema-name picker, be told a change is breaking, and be trusted to
confirm it. We do **not** design a hand-holding wizard, nor a permissions/RBAC model —
the editor is already a loopback, edit-time, single-operator tool (see the CSRF / DNS-
rebinding guards in `editor.py`), so "who is allowed to edit" is already "the operator at
the keyboard." The UX decisions below (block / warn / confirm) are calibrated to a
developer who understands what a contract is.

### Scope — the schema-NAME vs inline-schema fork (a primary decision, not a detail)

There is a structural fork in "edit a prompt's contract":

- **(a) Edit the schema NAME** in front-matter — pick from / type a name that resolves in
  the `rebar.schemas` registry (`inputs: reviewer_input`). This is the **current model**:
  heavy JSON Schemas are referenced by name, never inlined (§1.1 of the authoring guide).
- **(b) Edit schema CONTENT inline** in the front-matter — author the JSON Schema body
  itself inside the prompt file.
- **(c) A hybrid** — name-by-default, inline-as-escape-hatch.

**This ADR is scoped to (a).** Inline schemas (b) are explicitly **out of scope** and
**not recommended**: they would be a major new capability that does not exist anywhere
today (the whole contract model is "named schemas in one registry so the inspector and
the validator share one vocabulary" — §3). Inlining would fork the schema-source-of-truth,
defeat the registry-coverage and `$ref` resolution machinery (`schemas.registry()`),
and let every prompt grow a private, untested schema. If a future need for inline schemas
arises it is its own decision and its own ADR. Naming the fork here keeps the rest of this
document reasoning about a single, well-bounded solution space.

## The two questions

1. **Should a prompt's `inputs`/`outputs` schema NAMES be editable from the editor, and
   how — picking + validating against the registry, the override-drift interaction, the
   breaking-change UX, and content-hash + `schema_version` provenance?**
2. **Should a scripted-op (`@register_step`) contract ever be editable from the editor?**

## Decision

### 1. Prompt contract (schema names): YES — editable, as a guarded extension of the existing prompt-save path

Allow editing the front-matter `inputs:` / `outputs:` schema **names** from the editor's
existing prompt edit form (the surface 6592 shipped), with the safety model below. Do
**not** build a separate panel or a separate write path.

#### Schema-name picking and validation

- **Enumeration.** The editor offers the **registry contract schemas** as a closed
  pick-list: `rebar.schemas.CONTRACT_SCHEMAS` for inputs (the per-step I/O contract
  schemas — e.g. `reviewer_input`, `fetch_ticket_input`) plus the structured-output
  schemas valid as `outputs` (e.g. `review_result`, `completion_verdict`,
  `plan_review_verdict`). A new read endpoint (`GET /schemas`, token+Host guarded like
  `/prompts`) returns `{name, title/description, fields}` so the picker can show what each
  schema produces/consumes — reusing `editor_contracts._schema_fields`. **Free text is
  also accepted** (a developer may be authoring a step that targets a schema not yet in
  the curated list), but an unrecognized name is flagged (see the write-time rule).
- **Write-time validation rule (the decision rule the plan asked for).** On save, the
  chosen name is resolved via `rebar.schemas.load(name)`:
  - **resolves** → accept.
  - **does not resolve** → the save is **blocked** with a clear, fail-closed error
    (`unknown contract schema '<name>'; known: …`), mirroring the existing
    `validate_node_config` fail-closed posture (Save is already blocked while errors
    exist). It is never silently accepted (which would ship a prompt whose contract the
    validator can't load — exactly the "validation UNAVAILABLE" silent-degrade class ADR
    0006 exists to kill).
  - **empty / cleared** → accept (a prompt with no declared contract is valid — it is the
    UNKNOWN/contract-less state the inspector and runtime already tolerate). Clearing a
    contract is the explicit way to make a step contract-less.
- **Reuse the existing UI seam, don't invent one.** The editor already has the bpmn-js
  properties-panel `rebar:Config` provider for structured per-field step editing (the
  per-step `model:` field round-trips this way — see [workflow-authoring-v2.md §2.1](../workflow-authoring-v2.md)
  and the editor host's `window.REBAR_CONTRACTS`). The schema-name picker should be **two
  more fields on the existing prompt edit form / properties panel** (an `inputs` select +
  an `outputs` select, each backed by `/schemas`), not a new surface. This keeps one
  write path (`/prompt/save` → `save_prompt`), one canonical writer
  (`write_front_matter`), and one validation posture.

#### Override-drift gate interaction (mapping onto the EXISTING `prompt_override_drift`)

This is the crux, and there is an **existing safeguard we must map onto, not re-solve**:
`prompts.prompt_override_drift` (`prompts.py:564`, surfaced via
`lint._prompt_override_drift_findings`, `lint.py`, and `rebar workflow validate`) already
flags a project `.rebar/prompts/<id>.md` override whose `outputs` contract differs from
the built-in's, because *"a changed `outputs` contract on an override is BREAKING for
downstream steps."* That gate is **read/lint-only** — it fires *after the fact*, at the
next `validate` run. The plan correctly identified the gap: what does the editor do at
**write time**?

**Decision — surface the SAME gate inline, fail-closed on a breaking override.** The
editor must not grow a second, divergent breaking-change detector. Instead:

- On a contract edit to a prompt id that **also has a built-in counterpart** (i.e. the
  edit would create or change an override), the save handler calls the **existing**
  `prompt_override_drift`-equivalent comparison (factor the built-in-vs-edited `outputs`
  diff out of `prompt_override_drift` into a small pure helper both the linter and the
  editor call, so they can never diverge).
- An edit that would **change the `outputs` contract of an override** is treated as a
  **breaking change**: the editor surfaces it inline as a distinct, blocking state and
  requires an **explicit confirm** before writing (block-then-confirm — not silent-allow,
  not warn-and-forget). This matches the persona: a developer is told "this changes the
  `outputs` contract `X → Y`; downstream steps bound to the old shape will break —
  confirm?" and may proceed deliberately.
- A change to **`inputs`** is *not* gated by `prompt_override_drift` today (it only
  guards `outputs`, since downstream steps bind to the *producer's* output). We keep that
  asymmetry: an `inputs` change is shown but not blocked (it affects what the prompt
  *consumes*, validated at runtime against the resolved `with`, not a downstream binding).
  The ADR records this asymmetry explicitly so a later reader doesn't "fix" it into a
  spurious block.
- **Net:** the editor *triggers and surfaces* the existing gate at write time (so there
  is no confusing double-feedback where the editor is silent and `validate` later
  complains); it does **not** replace it. `rebar workflow validate` remains the
  authoritative CI-side check.

#### Breaking-change UX for existing consumers

Beyond the override-drift case, an `outputs`-name change on a prompt that workflows
already reference can break **consumer steps** that bind to its produced fields. The
editor already runs the shallow static contract check + the live `/validate` net
([§5](../workflow-authoring-v2.md)). The recommendation:

- After a contract edit, **re-resolve contracts for the open workflow** (`resolve_contracts`)
  and re-run the producer→consumer `shallow_contract_check` for steps wired to this
  prompt. A new **ERROR** (a consumer's required field now absent) is shown inline before
  Save, exactly like the per-node validator does today — **fail-closed Save**.
- This is best-effort across *other* workflows (the editor only has the open file in
  hand); the authoritative cross-workflow check stays `rebar workflow validate` in CI.
  The ADR does **not** claim to find every downstream consumer at edit time — it claims to
  not silently ship a break in the workflow you are editing, and to defer the repo-wide
  guarantee to the existing lint/CI gate.

#### Provenance: content-hash + readers-before-writers `schema_version` (an explicit invariant)

Editing a contract is just editing front-matter, so it flows through the same provenance
machinery — and the ADR records each as an invariant the implementation must preserve:

- **Canonical writer.** All writes go through `write_front_matter` (the parse-split-rejoin
  canonical writer): known keys in canonical order, **`schema_version` stamped**, body
  preserved byte-for-byte, idempotent, BOM-refused ([§1.3](../workflow-authoring-v2.md)).
  `inputs`/`outputs` are existing **known keys** in `FRONT_MATTER_KEYS`, so editing them
  needs **no `schema_version` bump** — the write-side policy bumps the version only when a
  writer begins emitting a key a prior version lacked, which this is not.
- **Readers-before-writers.** The read-side refuse stays the safety net:
  `parse_front_matter` rejects a prompt whose `schema_version` exceeds the running binary
  (`PromptVersionError`). Because contract editing emits no new key, an old binary reads an
  edited prompt fine. The discipline (deploy readers before writers) is unchanged; this
  ADR adds no new version surface.
- **Content hash.** The prompt's `content_sha256` (`prompt_content_hash`, embedded in the
  resolve trace) covers the **body** text, not the front-matter. A contract-only edit
  therefore does **not** change the body hash — which is correct (the *text* that ran is
  unchanged), but means contract provenance rides on the front-matter itself + the derived
  index. The ADR flags this so implementers don't assume the body hash tracks contract
  changes; the derived index (next bullet) is the contract-change provenance.
- **Derived index.** A **packaged** contract edit regenerates `reviewers/index.json` (the
  index records the contract surface), so the **CI drift gate** ([§4 / CI drift gate](../workflow-authoring-v2.md))
  catches a stale index. A **project override** edit does not touch the packaged index
  (correct — an override is not part of the packaged catalog), and is instead covered by
  the override-drift gate above. `save_prompt` already does exactly this regen-on-packaged
  branching; the contract edit inherits it for free.

#### Lifecycle + concurrency (CREATE / UPDATE / RETIRE, and races)

The plan asked the ADR to address the full shared-state lifecycle:

- **CREATE / UPDATE** — a contract is set/changed by writing the front-matter; no new
  path. The atomic write (`_atomic_write`: temp-file + `os.replace`) means a failed write
  never leaves a half-written prompt; the create-collision guard (`_prompt_exists`,
  refuse-unless-`overwrite`) already covers CREATE.
- **RETIRE** — clearing a contract (empty `inputs`/`outputs`) makes the step contract-less
  (UNKNOWN → validation skipped, the documented contract-less behavior). Removing a whole
  *prompt* retires it from the derived index on regeneration (existing behavior). There is
  no separate "contract version" object to garbage-collect — the contract is just keys on
  the file, so there is no orphaned-version cleanup path to design.
- **Concurrency.** The editor is loopback, single-operator, edit-time (one server, one
  browser session, per-session token). True multi-writer races are out of the threat
  model. The residual TOCTOU is *the operator's editor vs. an external process editing the
  same file* — handled by the existing **`.bak` backup on overwrite** (the IR save already
  backs up; the prompt save should too) and the atomic replace. We deliberately do **not**
  add an optimistic CAS-on-`schema_version` lock: it would be machinery for a race the
  tool's single-operator model doesn't admit, and `schema_version` is a format version,
  not a per-edit revision counter (overloading it would be a category error). The ADR
  records this as a *considered-and-rejected* option so it isn't reinvented.

#### Non-happy-path states (the consequential ones)

The implementation must specify, not just the happy path:

- **Invalid/unresolvable schema name** → fail-closed block + clear error (above).
- **Breaking-change detected mid-edit** → distinct inline blocking state + explicit
  confirm (override-drift) or fail-closed ERROR (consumer break) before Save.
- **Empty / partial contract** → empty is valid (contract-less); a half-typed name that
  doesn't resolve is treated as unresolvable (block) until corrected.
- **Abandoned / dirty edit** → discard is non-destructive (nothing is written until Save;
  the `.bak` protects the prior file on a confirmed overwrite). No autosave.
- **Validator unavailable** → the existing distinct "validation unavailable" state
  (`unavailable:true`) and fail-closed Save apply unchanged — never a false green.

### 2. Scripted-op (`@register_step`) contracts: NO — code is the source of truth

**Scripted-op contracts are NOT editable from the editor**, and this is a principled
boundary, not an omission. The positive rationale (so a future request to relax it can be
rejected on the merits):

- **There is no file artifact to edit.** An op contract is populated at **Python import
  time**: the `@register_step(input_schema=…, output_schema=…)` decorator runs on import
  and writes into the in-process `STEP_CONTRACTS` dict; `contract_for(name)` reads it back.
  Unlike a prompt — a `.md` file the editor can rewrite — the op contract has no on-disk
  representation the editor could open, edit, and write back without **editing Python
  source**, which is squarely outside an edit-time visual workflow editor's remit.
- **A runtime override would create a divergence.** If the editor wrote a contract
  override somewhere (a sidecar file, a per-workflow override), the *editor view* and the
  *actual runtime contract* (`STEP_CONTRACTS`, what the interpreter validates against)
  would diverge — reintroducing exactly the implicit/weakly-enforced-seam silent-degrade
  class ADR 0006 was written to eliminate. The op contract is load-bearing for runtime
  input validation (`input contract violation (<schema>)`); it must have one source.
- **The op's behavior and its contract are the same change.** An op's contract changes
  *because its code changes* (it now reads a new field, returns a new shape). Editing the
  contract without editing the code is meaningless — and editing the code is a PR, with
  the registry-coverage test (every registered op carries a contract) and CI behind it.

The boundary is therefore: **prompts are data the editor authors (text in 6592, contract
names here); ops are code the editor only *inspects*.** The editor keeps showing op
contracts read-only via `step_contract_view` (CONSUMES/PRODUCES/description) — viewing is
in scope, editing is not. The inspector's empty-state and "⚠ unchecked (opaque source)"
affordances are unchanged.

## Options considered (and why rejected)

| Option | Summary | Verdict |
|--------|---------|---------|
| **A. Schema-NAME editing on the existing prompt form** (recommended) | Two picker fields (`inputs`/`outputs`) on the 6592 prompt-save path, registry-resolved, drift-gate-surfaced. | **Chosen** — minimal new surface, one write path, reuses canonical writer + derived index + override-drift + validator. |
| B. Inline schema CONTENT in front-matter | Author the JSON Schema body in the prompt file. | Rejected — forks the schema source of truth, defeats the registry/`$ref`/coverage machinery, untested private schemas. Out of scope (a separate ADR if ever needed). |
| C. Separate "contract editor" panel | A new bpmn-js panel / new endpoints distinct from the prompt form. | Rejected — duplicates the write path and validation posture; the existing `rebar:Config` properties-panel provider already hosts structured per-field step editing. |
| D. Editable op contracts (editor writes an override) | Let the editor override `@register_step` contracts. | Rejected — no file artifact; creates editor-view-vs-runtime divergence (the ADR 0006 anti-pattern); op contract changes are code changes. |
| E. Optimistic CAS-on-`schema_version` concurrency lock | Treat `schema_version` as a per-edit revision and CAS on write. | Rejected — single-operator loopback tool has no multi-writer race; `schema_version` is a format version, not a revision counter. `.bak` + atomic replace suffice. |

## Consequences

- The editor's prompt-edit form gains `inputs`/`outputs` schema-name pickers (closed
  registry list + free-text + write-time resolve-check). One write path, one canonical
  writer, one validation posture — all reused.
- A small pure helper is extracted from `prompt_override_drift` so the editor's write-time
  breaking-change surfacing and the lint-time gate share **one** comparison and can't
  diverge.
- Op contracts remain code-only and read-only in the editor; the boundary is documented so
  future "make ops editable" requests are answered by this ADR.
- No `schema_version` bump and no new front-matter key — readers-before-writers and the
  content-hash story are unchanged.

### Follow-on implementation stories (identified from the chosen approach)

1. **`GET /schemas` registry endpoint + picker data** — token+Host-guarded endpoint
   returning the selectable contract-schema names + their field views (reusing
   `editor_contracts._schema_fields`); enumerated from `schemas.CONTRACT_SCHEMAS` +
   the structured-output schemas valid as `outputs`.
2. **Schema-name picker fields on the prompt edit form** — two select+free-text fields in
   the `rebar:Config` properties panel, wired to `/schemas`; round-trip through
   `/prompt/save` → `save_prompt`.
3. **Write-time resolve-check + fail-closed save** — block save on an unresolvable schema
   name (mirroring `validate_node_config`'s fail-closed posture); accept empty (clear).
4. **Factor `prompt_override_drift` into a shared helper + inline breaking-change confirm**
   — one comparison used by both the linter and the editor; block-then-confirm on a
   breaking `outputs`-override edit; surface a consumer-break ERROR via the open workflow's
   `shallow_contract_check`.
5. **`.bak` backup on prompt overwrite** — parity with the IR save's backup, closing the
   external-process TOCTOU.
6. **Docs + tests** — extend [workflow-authoring-v2.md §1.4 / §7](../workflow-authoring-v2.md)
   and the editor docs; unit-test the picker enumeration, the resolve-check decision rule,
   and the shared override-drift helper without a browser.

> **Prompt-TEXT editing is already delivered** (story 6592, closed under epic `da27`):
> the prompt library, in-UI create/edit, auto-detected atomic write-back, and derived-index
> regeneration. This ADR covers only the **contract-name** half that remained read-only.
