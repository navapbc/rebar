# ADR 0116 — Retire config aliases by clean pre-1.0 removal

**Status:** Accepted
**Date:** 2026-09-04

## Context

The config surface currently treats eight old environment/config spellings as permanent ergonomic
aliases. `src/rebar/_deprecations.py` records them as `_permanent(...)` entries,
`src/rebar/_config_sources.py` resolves the environment aliases, and
`src/rebar/_config_sections.py` resolves the `verify.overlap_enabled` TOML alias.

Authorizing story `038d-f5ff-b371-4ba5` received the operator decision for this cleanup: remove these
aliases for simplification before 1.0 because there are few clients and none are expected to be
using the old names. That decision rejected converting these aliases into fail-loud tombstones as
the governing policy. The follow-on chain therefore needs one ADR that records the approved
compatibility boundary before implementation and publication tickets proceed.

The dependent work under `038d-f5ff-b371-4ba5` is sequenced so this ADR lands first;
`7370-0274-9f80-437d` then changes the removed-input handling according to this policy; the
runtime alias declarations and parsing/warning paths are removed as part of the 038d chain; and
`1a63-056e-98b9-4ab7` finally publishes canonical config docs and release notes.

## RESEARCH findings

- `src/rebar/_deprecations.py` currently lists seven environment aliases and one config-key alias
  as permanent aliases, not scheduled removals.
- `src/rebar/_config_sources.py` maps the environment aliases only when the canonical environment
  variable is unset, and gives canonical variables precedence.
- `src/rebar/_config_sources.py` gives `REBAR_NO_SYNC` transformed semantics: a truthy legacy value
  maps to `REBAR_SYNC_PULL=off`, while a falsy value maps to `on`.
- `src/rebar/_config_sources.py` gives `REBAR_ID_GUARD_MODE` transformed semantics: `warn` maps to
  `REBAR_UNSAFE_ID_GUARD_BYPASS=true`, while `raise` and other values map to `false`.
- `src/rebar/_config_sections.py` maps `verify.overlap_enabled` to
  `verify.suggest_duplicate_tickets` through `_ALIASES`.
- `docs/api-stability.md` distinguishes config aliases from stronger compatibility contracts:
  structured JSON/MCP schemas, the event schema, the public Python facade, and persisted-data
  compatibility exceptions.
- The operator approval on `038d-f5ff-b371-4ba5` authorizes clean removal for simplification with no
  expected clients and rejects fail-loud tombstones as the default migration rationale for these
  aliases.

## Decision

Retire the following eight aliases before 1.0 by clean removal of their alias declarations and
active parsing/warning paths:

| Retired alias | Canonical replacement |
| --- | --- |
| `REBAR_NO_SYNC` | `REBAR_SYNC_PULL=off` |
| `COMPACT_THRESHOLD` | `REBAR_COMPACT_THRESHOLD` |
| `SCRATCH_BASE_DIR` | `REBAR_SCRATCH_BASE_DIR` |
| `REBAR_ACLI_TIMEOUT` | `REBAR_JIRA_CLI_TIMEOUT` |
| `RECONCILER_ABSENT_GET_BUDGET` | `REBAR_RECONCILER_DELETION_PROBE_LIMIT` |
| `REBAR_ID_GUARD_MODE=warn` | `REBAR_UNSAFE_ID_GUARD_BYPASS=true` |
| `REBAR_VERIFY_OVERLAP_ENABLED` | `REBAR_VERIFY_SUGGEST_DUPLICATE_TICKETS` |
| `verify.overlap_enabled` | `verify.suggest_duplicate_tickets` |

This retirement is an operator-approved pre-1.0 compatibility break, not a new general rule that
all renamed config keys may be removed immediately. The rationale is narrow: these aliases were
kept as ergonomic renames, the project has few clients, no clients are expected to depend on the
old names, and keeping every old spelling permanently makes the config layer larger and harder to
reason about.

Follow-on implementation removes these aliases cleanly. It must not keep them as permanent aliases,
and it must not convert them into fail-loud tombstones merely because they once existed. A tombstone
or hard error remains appropriate only when a separate implementation plan identifies a still-set,
load-bearing removed input that would otherwise be silently unsafe.

`REBAR_NO_SYNC` and `REBAR_ID_GUARD_MODE` need explicit migration text because their replacements
are not identity mappings: one is an inverse pull switch and one converts a mode string into a
boolean unsafe-bypass flag. The other six aliases are inert spelling renames and should be removed
under the same clean-removal policy.

This decision does not weaken rebar's stronger compatibility promises for public/wire/persisted
surfaces. JSON output schemas, MCP schemas, the event schema, the documented public Python facade,
and persisted-data migrations/readers remain separately compatibility-bearing and must continue to
justify any shim or adapter that protects them.

## Research critique and resolution

1. **The old registry labels these aliases permanent.** That is the state being changed, not a
   constraint against change. **Resolution:** record this ADR as the governing pre-1.0 decision and
   let the implementation tickets remove the old permanent-alias entries.
2. **Some prior removed inputs use tombstones.** Tombstones protect load-bearing removed inputs from
   silent unsafe fallback. **Resolution:** do not generalize that mechanism here; the operator
   approved clean removal for these aliases because no clients are expected.
3. **Two aliases have transformed semantics.** Silent removal without migration text could confuse
   operators. **Resolution:** the ADR names both transformations and requires publication work to
   include explicit migration text.
4. **The config stability page promises alias windows.** A reader could treat that as forbidding
   this cleanup. **Resolution:** update `docs/api-stability.md` to state that ADR-recorded,
   operator-approved pre-1.0 alias removals are the exception, while strong public/wire/persisted
   contracts remain separate.

## Consequences

- The config layer can delete eight legacy spellings instead of preserving permanent aliases.
- Operators who still use one of these aliases must migrate to the canonical spelling before the
  removal release; release notes and config docs must name every mapping.
- Follow-on work has a clear sequencing rule: land this ADR first, then remove the alias behavior,
  then publish canonical docs and release notes.
- The decision is intentionally narrow and cannot be cited to remove JSON/MCP/event/public-facade or
  persisted-data compatibility without a separate contract-specific decision.
