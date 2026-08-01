# ADR 0057 — Model classes are the interface; `REBAR_LLM_MODEL` is deprecated

**Status:** accepted
**Ticket:** d23e (parent epic: the LLM provider seam)

## Context

rebar's LLM operations used to choose a model with one scalar knob, `REBAR_LLM_MODEL`. That is
too coarse for how the operations actually differ. A plan review's Pass-1 is open-ended
reasoning; a completion verifier's yes/no check is decisive and narrow. Forcing both through a
single model means either paying frontier prices for a decisive check or degrading the
open-ended one.

The seam introduces three **model classes** — `trivial`, `standard`, `frontier` — configured as
slots:

```toml
[tool.rebar.llm.model_classes]
frontier = { model = "anthropic:claude-opus-4-8" }
standard = { model = "anthropic:claude-sonnet-4-6" }
```

Once classes are the interface, a bare `REBAR_LLM_MODEL` is a **second, ambiguous way to say the
same thing**: it names a model without saying which work it is for.

## Decision

`REBAR_LLM_MODEL` is **deprecated with a migration window**, not kept as a permanent alias.
Within the window it keeps working and **fans out to all three classes**, warning when read.

### Why a window rather than a permanent alias

`_deprecations.py` distinguishes two kinds of deprecation, and the difference decides this:

- `_permanent(...)` — for a **rename**. The six existing env entries are permanent because they
  are "stable `REBAR_`-prefixed renames of established names": the same knob under a better name,
  so removing them would buy nothing.
- `_scheduled(...)` — for a surface that is **meant to go away**.

`REBAR_LLM_MODEL` is not a rename. It is **superseded** by a different interface. The six
`_permanent` entries are therefore precedent for renames, **not** for supersessions, and counting
them is the wrong way to resolve this question. Two earlier drafts of the implementing plan got
this wrong by counting precedents instead of reading why they exist.

The fan-out is the **widest-compatibility** reading: an operator who set only the old knob has it
honoured everywhere rather than for one arbitrary class.

### Where the deprecation warning fires

At **every environment read of the variable, and nowhere else** — two sites:

- `llm/config.py` — the `_llm_str(..., "REBAR_LLM_MODEL", ...)` read in `from_env`;
- `llm/model_classes.py` — the read the fan-out needs.

It deliberately does **not** live in `resolve_model_string`, even though every path reaches it.
`resolve_model` passes an **already-resolved string**, where the value is indistinguishable from a
config-table `model` key or a per-step `model:`. A warning there would fire at operators who never
set the variable at all. Provenance exists only at the env read.

Emission is **per call**. `warn_deprecated` has no deduplication and no sibling env deprecation
has any, so adding per-process state for this one surface would make it the odd one out. The read
is hoisted to the function that loops the classes, so one slot build emits one warning rather than
three.

### Precedence

CLI > per-class env (`REBAR_LLM_<CLASS>_MODEL`) > config table > bare `REBAR_LLM_MODEL` >
built-in default. An explicit class configuration always wins; the deprecated variable sits at the
default position.

The variable warns whenever it is **set**, even when a CLI `--model` overrides it. `_llm_str`
returns early on a CLI hit and never reads the environment, so warning only when the env value
*wins* would require either teaching that shared helper about one of its dozen variables or
duplicating its precedence at the call site. An exported deprecated variable is a migration item
regardless of what overrode it on a given run.

## Alternatives rejected

- **Keep it as a permanent alias.** Rejected: it is a supersession, and keeping both interfaces
  forever leaves two ways to say the same thing — the ambiguity this seam exists to remove.
- **Remove it immediately.** Rejected: it is the documented interface in the README and in
  several guides, and there are live configurations using it.
- **Map it to `frontier` only.** Rejected: it would silently change behaviour for the decisive
  checks, and an operator who set one model plainly meant "use this".
- **Warn inside `resolve_model_string`.** Rejected: provenance is already lost there, so it would
  produce false warnings (see above).

## Consequences

- A runbook that names `REBAR_LLM_MODEL` as a recovery lever will rot when the variable is removed
  at v1.0.0. Recovery procedures should be expressed in class terms. This is tracked on the
  cutover story rather than changed here, because rewriting a production recovery path is a
  separate decision.
- `docs/env-vars.md` is generated, and records the entry as a deprecated alias with its removal
  milestone automatically, because the env read is a string literal the generator can resolve.
