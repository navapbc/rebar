# Documentation policy

This policy defines ownership, lifecycle, canonical sources, correction methods, and writing guidance for maintained documentation and durable records. Authors classify each surface before changing it and correct the canonical source named below.

## Documentation roles

The lifecycle classes are living reference, policy, design rationale, and historical evidence.

| Role | Primary audience | Purpose | Lifecycle | Canonical source | Citation use | Correction method | Exclusions | Example |
|---|---|---|---|---|---|---|---|---|
| Tickets | Future developers and agents | Preserve plans, progress, decisions, and verification | Historical evidence | The append-only ticket event stream | Cite a ticket when its history explains maintained context | Add a new append-only correction event and link an adjacent notice when needed | Current guidance and duplicated design rationale | A ticket records a plan and completion evidence |
| ADRs | Future developers and agents | Preserve decisions that establish architectural invariants | Design rationale | The accepted ADR and its recorded status | Cite an ADR when a maintained invariant needs its rationale | Add substance-preserving annotations or errata, record supersession, or create a replacement ADR | Current operating instructions and erased decision substance | An ADR explains an architectural boundary |
| Internal documentation | Contributors, operators, and agents | Explain current development processes, architecture, and code | Living reference or policy | The maintained page aligned with implementation and configuration | Reference tickets or ADRs when historical context is needed | Correct the current-state canonical source | Client instruction and repeated history | The architecture guide describes current subsystem boundaries |
| External documentation | Clients who do not develop rebar | Teach supported use of rebar | Living reference | The maintained client page aligned with supported behavior | Cite other client documentation when it provides needed detail | Correct the current-state canonical source | Implementation history and contributor procedure | The user guide explains ticket workflows |
| Shipped help | Clients using an installed rebar package | Support discovery and direct users to shipped guidance | Living reference | The packaged help source | Cite maintained client documentation when it provides needed detail | Correct the canonical help source and regenerate derived help | Internal implementation and historical narration | Command help explains an option |
| Comments | Developers and agents reading code | Explain context required to understand code | Living reference | The comment beside the relevant code | Cite a ticket or ADR only when it supplies necessary context | Update or remove the comment with the code | Conversations, historical records, and code restatements | A comment explains a non-obvious invariant |
| Generated artifacts | Contributors and operators | Provide reproducible projections from maintained sources | Living reference | The generator source and declared inputs | Cite the artifact catalog for ownership and regeneration details | Correct the generated source and regenerate the output | Direct edits to derived output and ephemeral build output | The CLI reference is rendered from command sources |
| Protected evidence | Future developers, auditors, and investigators | Preserve captured material and provenance | Historical evidence | The original artifact and its adjacent provenance metadata | Cite stable records that establish origin and interpretation | Add an adjacent correction or provenance record without altering the evidence | Current guidance and unmarked interpretation | A frozen fixture preserves a recorded response |

## Correction rules

Maintained current-state sources receive corrections in place. Authors update the canonical source before any derived output. Generated artifacts receive corrections through their generator source and regeneration path.

Ticket events remain append-only. Authors correct a ticket with a new append-only event and retain every earlier event.

ADR decision substance remains intact. Authors use substance-preserving annotations or errata, record supersession, or create a replacement ADR. They do not erase the earlier decision.

Protected evidence remains unchanged. Authors place corrections or provenance metadata beside the evidence and preserve the original artifact.

Authors cite tickets or ADRs when historical context is needed. They do not restate that history in current-state documentation.

## Structural documentation checks

`scripts/check_docs_index.py` preserves two repository contracts. It requires every top-level `docs/*.md` file other than `docs/README.md` and `*.local.md` files to appear as a Markdown link in `docs/README.md`. It also validates inline Markdown links and images from a declared maintained-source boundary.

The link checker scans these Markdown sources:

- Root `*.md` files.
- `.agents/**/*.md`.
- `.github/**/*.md`.
- `docs/**/*.md`.
- `examples/agent-skills/**/*.md`.
- `infra/runbooks/**/*.md`.
- `infra/**/README.md`.
- `scripts/**/README.md`.
- `src/rebar/_guides/**/*.md`.
- `src/rebar/llm/eval_specs/**/*.md`.
- `templates/**/*.md`.
- `tests/external/**/README.md`.
- `tests/unit/fixtures/README.md`.

The source boundary excludes these Markdown sources:

- `.joe-janitor/**/*.md`.
- `.rebar/prompts/**/*.md`.
- `src/rebar/llm/reviewers/**/*.md`.
- `tests/fixtures/**/*.md`.
- `tests/scripts/fixtures/**/*.md`.
- `tests/unit/rebar_reconciler/integration_gates/**/*.md`.
- Dot-prefixed root Markdown files.
- Files ending in `.local.md`.

An excluded source may remain a valid link target. Each relative target resolves from the source directory and may refer to any existing path inside the repository. The checker removes query strings and fragments before file resolution. A missing target or a target outside the repository produces a sorted finding with the source path, line number, raw target, normalized target path, and reason.

The parser scans inline links and images outside fenced blocks and inline code spans. It ignores scheme-qualified targets, fragment-only targets, heading anchors, external URL availability, reference-style links, HTML links, and bare path mentions.

The shared documentation action invokes this checker from full verification and the Gerrit documentation-only route. The checker does not analyze punctuation, diction, tone, audience, canonical-source ownership, or any other writing guidance in this policy.

## Writing rules

These rules apply to new or edited maintained text.

They are authoring guidelines. They do not define a deterministic validator or gate contract.

Do not use the em dash, en dash, spaced hyphen connector, semicolon, or clause-joining colon.

Use a colon only to introduce an item list or as a structural label. Keep spoken narration free of colons.

Use hyphens only in compound words, technical names or identifiers, Markdown list bullets, table separators, horizontal rules, and code or SQL operators. Do not use a plain hyphen as a dash.

Keep every Markdown paragraph on one logical line. Use line breaks only when Markdown structure requires them.

Write formal complete sentences with precise verbs. Do not use contractions, colloquialisms, casual connectors, filler, or authenticity padding. Leave a noun unmodified unless an adjective selects one option from several.

The default banned words and phrases are: `real`, `actual`, `genuine`, `live` when it acts as a value adjective, `simply`, `just`, `quietly`, `seamlessly`, `robust`, `powerful`, `leverage`, `truly`, `in real time`, `concretely`, and `today` when it adds emphasis rather than a date.

The default authenticity padding phrases are: `not a placeholder`, `live for this run`, `the thing that matters`, and `this is not a mock`.

Describe the mechanism or source instead of claiming authenticity.

## Protected forms

These writing rules do not authorize changes to licenses, quotations, captures, frozen output, serialized fixtures, technical syntax, wording fixtures, generated outputs, historical ticket events, ADR decision substance, or protected evidence. Corrections follow the method assigned to each role.
