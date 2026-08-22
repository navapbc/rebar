# Documentation policy

This policy defines ownership, lifecycle, canonical sources, and correction methods for maintained documentation and durable records. Authors classify each surface before changing it and correct the canonical source named below.

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
