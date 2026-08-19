# ADR 0100 — Lazy CLI command and capability registry with canonical help

**Status:** Accepted
**Date:** 2026-08-19
**Ticket:** `educative-hygienic-dore` / `4afa-216b-4b16-4b6b`
**Epic:** `grapy-cynical-copepod` / `09ae-e827-1cac-452d` (RP-05)
**Relates to:** [ADR 0098](0098-operation-scoped-config-and-provider-composition.md)
(RP-04: the operation snapshot / config / provider-composition boundary this ADR routes
*before*) and [ADR 0070](0070-jira-onboard-config-write.md) (how `rebar bridge setup` /
its `jira-onboard` compatibility spelling persists config — the historical spelling this
ADR preserves).

## Context

Rebar's `rebar` entrypoint is an in-process argparse CLI. On current `origin/main`
(`09947b1f`) the facade in `src/rebar/_cli/__init__.py` reconstructs command identity and
initialization/mount policy through ~19 overlapping `frozenset`s (`_INTERCEPTS`,
`_READS_INIT_ONLY`, `_WRITES_FULL`, `_BRIDGE`, `_HIDDEN_ALIASES`, …), a pure-intercept
dispatch ladder, per-command package-help filenames under `src/rebar/_cli/help/`, and
repeated census assertions in tests. Command *syntax* has no single authority: most core
ticket/store handlers parse `argv` imperatively, a minority of families
(`_bridge_commands`, `_jira_onboard`, `_llm_commands`, `_llm_eval_commands`,
`_workflow_commands`, and six standalone command modules) already build `argparse`
parsers, and `scripts/gen_cli_reference.py` embeds 54 hand-maintained package-help files
plus a curated 22-command intercept table and regex-parses the facade. That generator
detects **census** drift (a command missing a help file), not **parser/help** drift: an
option can change in a handler's imperative parser while the committed help and the
generated reference stay stale and still pass.

The surface is also inconsistent for the operator. Twenty-one intercept-only advanced
verbs (`enrich`, `explain`, `review-plan`, `verify-*`, `remote-cert`, `trusted-env`,
`workflow`, `llm`, `prompt`, `criteria`, `identity`, `audit`, `config`, `metrics`, …) are
**absent from the overview** and report *unknown* under `rebar help <command>`, while
their handler-owned `--help` is inconsistent and can execute real work before printing
(`enrich --help` was not side-effect-free). The maintainer approved, during the RP-05
brainstorm and design (durable on research ticket `rubellite-secondary-cub` /
`ee98-a2c0-7d12-4c75`; local note `.joe-janitor/rp05-cli-registry-research.md`),
normalizing help for **every** visible top-level command and deriving syntax/options from
the **same** parser definitions execution uses.

This is a structural, migration-bearing decision with a compatibility surface (generated
help becomes a public boundary; 21 help behaviors change intentionally), so its rationale
is recorded here before implementation. It does not contradict ADR 0098: RP-05 owns only
routing/help/identity *before* the operation boundary; ADR 0098's operation snapshot,
config materialization, and provider bindings remain the sole authority *after* it.

## Decision

### 1. One stdlib-only immutable lazy route registry

A single immutable registry (frozen dataclasses + typed enums, stdlib only) is the sole
authority for top-level command identity and policy. Each route records: canonical
identity; accepted spellings and visibility (canonical / exact-alias / compatibility /
hidden / retired); a **lazy** handler reference and a **lazy** parser-factory reference,
each a `module:function` string resolved only when needed; a **bounded** invocation-adapter
kind (a closed enum whose members are exactly the distinct handler invocation signatures
observed across the migrated handlers — e.g. in-process reader, in-process leaf-write,
subprocess-launch — validated by the registry's own unit tests); the generated
package-help key; mount/init policy; confirmation/output policy; and the set of semantic
capabilities the route may exercise. The registry is a **routing table**, not a plugin
system, DI container, or middleware framework (see *Alternatives considered* and the
Rule-of-Three posture against premature registry abstractions). Duplicate spellings, policy contradictions, unknown
capability names, missing help resources, and retired-token collisions are rejected by
**pure runtime validation** at registry construction; full `module:function` resolution is
isolated to the full-extra CI contract lane, never ordinary startup (which would defeat
lazy imports).

### 2. Side-effect-free stdlib argparse factories are the single grammar source

Every command grammar is built by a side-effect-free stdlib `argparse` factory that
accepts a deterministic `prog`. **Runtime handlers and build-time help generation call the
same factory.** Existing argparse builders are extracted into lean parser modules;
imperative parsers are migrated with their accepted/rejected `argv`, `--` handling,
diagnostic messages, streams, and exit codes pinned by production-path compatibility
(“shadow parity”) tests **before** their generated help becomes authoritative. A narrow
shared `ArgumentParser` subclass/formatter may preserve Rebar's diagnostics. There is **no
docs-only parser, no source-regex grammar, no Click dependency, and no second parser.**
A parser-family migration switches the runtime path *and* its tests to the factory as a
unit; a shipped phase must never contain a docs-only grammar the runtime ignores.

### 3. Deterministic generation of committed, runtime-authoritative help

Generation resolves all parser factories at build/check time under fixed `prog`, width,
and formatter policy, then commits the per-command package-help bytes and the grouped
overview bytes. **Runtime overview/help reads only those committed bytes** — it does not
reflow or re-render at runtime. Curated prose and worked examples remain editorial
supplements layered around the generated syntax/option tables; they may **not** define an
unchecked usage or option table. The generator's `--check` mode proves the committed
package-help/overview bytes and `docs/cli-reference.md` are byte-current against the
parser-rendered artifacts.

### 4. A tiny lexical top-level selector, distinct from handler argument parsing

The top-level selector parses only enough global syntax to identify the spelling and the
first-token help/unknown forms, and to reject structurally malformed *global* syntax. It
is routing, not a competing argument grammar. **Nested/subcommand argument parsing remains
owned by the selected handler** (e.g. `bridge preview --help` is served by the bridge
handler, not the top-level selector); the registry does not duplicate nested parsing, and
nested help must itself be side-effect-free and lean-tested.

### 5. Pre-operation help / alias / unknown routing

Overview, canonical top-level help, accepted-alias help, and unknown-command rendering are
**pre-operation** routing. These paths run before the RP-04 operation snapshot is composed,
before config discovery/materialization, before any store mount, before handler
resolution, and before any optional-package import. Real execution crosses the
operation-snapshot/config boundary afterward. This keeps `rebar --help`, `rebar help
<command>`, and `rebar <command> --help` working on a clean no-extras wheel and never
creating repository state (the fresh-clone regression, bug `dd62`).

### 6. Alias, hidden, and retired-spelling rules

- **Canonical** names appear exactly once in the grouped overview.
- **Exact-spelling aliases** reuse the canonical grammar and canonical package-help bytes;
  aliases are embedded per-route (no alias chains) and are not duplicated in the overview.
- **Compatibility shims** with a distinct historical invocation (`bridge-fsck`,
  `bridge-probe`, `jira-onboard`) compose the same argument-definition helpers with an
  alias-specific `prog` and a replacement-naming epilog; `jira-onboard` keeps its
  historical spelling.
- **Hidden aliases** (`bridge-status`) resolve explicit canonical help but stay absent from
  overview, completion, and discovery.
- **Retired spellings** (`purge-bridge`) have neither route nor help and remain unknown.

Alias metadata and retirement are validated centrally by the registry.

### 7. One semantic capability registry (descriptive, enforced late)

A separate registry maps each semantic capability to its packaging extra and a missing
posture — one of `error`, `unavailable`, `abstain`, or intentional `fallback`. Routes
**advertise** the capabilities they may exercise; the registry is descriptive, shared,
error-shaping infrastructure only. Capability checks occur **after** the selected
mode/backend is known — never as eager optional imports at routing time — and domain
components (metrics, grounding, rich-text) retain their existing degraded/fallback result
shapes.

### 8. Compatibility contracts for execution

Help/unknown paths never compose an operation or mount a store. Execution retains the
central best-effort store mount, strict per-arm init, confirmation and global-output
behavior, legacy JSON output shapes, token handling after `--`, and the established streams
and exit codes. The only accepted grammar change is the separately approved safe-help
normalization for the 21 intercept-only verbs; any other intentional delta must be named by
its own ticket. `rebar-mcp` remains a separate entry point and is not exposed as a `rebar`
verb.

### 9. Expand/contract migration and code-only rollback

Migration is expand/contract by command family: add the registry/parser contract and
derived compatibility exports; migrate each handler and its runtime parser together;
generate authoritative artifacts only after the complete parser/route census passes; then
move execution, capability boundaries, and docs/distribution consumers onto the registries;
and finally remove the compatibility authorities. No shipped phase contains a docs-only
grammar the runtime ignores. During expansion, rollback selects the derived compatibility
exports and reverts a parser family together with its generated artifacts; after
contraction, a code revert restores the old router. Ticket, event, bridge, and binding
formats do **not** change; there is no stored-data migration.

## Alternatives considered

- **Plain argparse subparsers + a small dispatch dict.** Rejected as the *primary*
  authority because the 21 intercept-only verbs and the mount/init/confirmation/capability
  policy are not expressible as subparser wiring alone, and lazy per-command imports must be
  preserved (eagerly building every subparser at startup would import optional backends).
  The immutable registry *wraps* argparse factories rather than replacing them — argparse
  remains the grammar engine.
- **Click / Typer.** Rejected: adds a third-party dependency and its own parsing/help model,
  duplicating argparse behavior Rebar already depends on for diagnostics, exit codes, and
  `--` handling, and complicating the clean no-extras wheel.
- **Parser-derived doc tools (`jsonargparse`, `func_argparse`).** Rejected as a dependency
  but adopted in spirit: the same-source principle (one parser definition feeds both
  `parse_args()` and `format_help()`) is implemented with stdlib argparse, matching CPython
  argparse, Django `BaseCommand.create_parser()`, Click's single parameter graph, and
  Cobra's command-tree doc walk.
- **A docs-only grammar or source-regex reference generator (status quo).** Rejected: it
  detects census drift, not parser/help drift, so option changes ship stale help. This ADR
  makes the parser the single syntax authority.
- **A plugin/DI/middleware framework.** Rejected as over-repackaging (Rule-of-Three): a
  fixed, in-tree, immutable routing table meets the need without runtime discovery.

## Verification obligations

- The document oracle is the ADR-number bijection plus a byte-current generated index:
  `python scripts/check_adr_numbers.py` and `python scripts/gen_adr_index.py --check`.
- The architecture oracle is cross-consistency with the approved research on
  `rubellite-secondary-cub`, the parser/runtime same-source decision, the current
  help/mount/output contracts, and ADR 0098; this ADR introduces no behavior change of its
  own — it records the boundaries the implementation stories deliver.
- ADR 0099 was already allocated (binding-store internal ownership, RP-02), so this ADR
  takes the lowest free number, 0100, and preserves the approved slug
  `cli-command-and-capability-registry`.

## Consequences

**Positive.** One syntax authority eliminates parser/help drift; every visible command has
safe, side-effect-free, pre-operation help; the overview enumerates every canonical public
verb exactly once; reference/distribution checks enumerate the registries instead of source
regexes; lazy imports and the clean no-extras wheel are preserved.

**Negative.** A new registry module and per-family parser factories are added; every
imperative parser must be migrated under shadow-parity tests before its help becomes
authoritative, which is the bulk of the epic's work.

**Neutral.** argparse remains the grammar engine; the alias/hidden/retired taxonomy
generalizes the existing compatibility-alias behavior; `rebar-mcp` and the reconciler
entrypoints are untouched.

## Research sources

Internal: research ticket `rubellite-secondary-cub` / `ee98-a2c0-7d12-4c75` (recovery
authority `irascible-clueless-basenji`) and the ignored local note
`.joe-janitor/rp05-cli-registry-research.md`, audited 2026-08-18. External prior art
inspected at then-current upstream heads: CPython `argparse` (one action set for
`parse_args()` and `format_help()`), Django `BaseCommand.create_parser()`, Click's single
parameter graph, Cobra's Markdown command-tree docs, and pip/kubectl/Git command-overview
conventions. No credential values or local credential stores were read during that
research.
