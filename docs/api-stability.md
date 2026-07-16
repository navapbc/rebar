# API stability

`nava-rebar` is versioned **0.x**. Under SemVer, 0.x means the public API may still
change between minor versions — while below 1.0, any minor release may carry breaking
changes — but "may change" is not the same for every surface. (For the current version
and what changed in each release, see the [CHANGELOG](../CHANGELOG.md) and the
[GitHub Releases](https://github.com/navapbc/rebar/releases).) This page states, per
surface, **what you can depend on today** and how changes are communicated, so you can
build against rebar without guessing which parts are load-bearing.

> **Pre-1.0 caveat.** Until 1.0, anything on this page **except the surfaces
> explicitly called out as a strong contract** may change in a minor release.
> The strong contracts (the `--output json` schemas and the event wire format)
> are already treated as compatibility-bearing and will not break lightly even
> pre-1.0. After 1.0 the whole matrix below becomes a SemVer promise: breaking
> changes only on a major bump, with a deprecation window first.

## Stability matrix

| Surface | Stability today | How changes are communicated |
|---|---|---|
| **CLI command names & options** | Stable in practice; the golden-path commands (`init`, `create`, `claim`, `transition`, `ready`, `list`, `search`, `show`) are settled. | Post-1.0: no removal/rename without a deprecation window. New flags are additive. `rebar --help` + the per-command help are authoritative. |
| **`--output json` schemas** | **Strongest contract.** The canonical [JSON Schemas](../src/rebar/schemas/) back every structured output and are the same schemas advertised to MCP clients as `outputSchema`s. | Backward-compatible evolution only: new **optional** keys may be added; required keys are not removed or retyped. Outputs are open (`additionalProperties: true`) so adding a key never breaks a reader. See [docs/output-schemas.md](output-schemas.md). |
| **Python `rebar.*` facade** | Documented **stable subset**: the public functions re-exported from `rebar` (`rebar.__all__`) and the return `TypedDict`s in [`rebar.types`](../src/rebar/types.py). The exception surface `RebarError` / `ConcurrencyError` is stable. | `_`-prefixed names (`rebar._*`) are private and may change at any time. Return types track the JSON-schema contract above (a drift-gate keeps them in sync). |
| **MCP tool names & input/output schemas** | Stable **subset** — the documented tool set in [CLAUDE.md](../CLAUDE.md) and their typed `outputSchema`s. | Deprecations mirror the CLI. Output schemas share the same `--output json` schemas, so the backward-compatible rule above applies. |
| **Event schema (the on-disk/wire format)** | **Strong contract.** The `tickets`-branch event log is a forward-compatible append-only format. `SCHEMA_VERSION` (in `rebar/reducer/_version.py`) gates it; unknown event types are **preserved-and-ignored** by older clients so mixed-version fleets converge. | New event types are additive; older binaries ignore them (and `fsck` WARNs when the store holds newer types). NDJSON export carries its own `EXPORT_SCHEMA_VERSION` (in `rebar/_io/export_ndjson.py`). See [docs/event-schema.md](event-schema.md). |
| **Config keys (`rebar.toml`, env vars)** | Stable keys; renamed keys keep a **deprecated alias**. | Aliases are documented with a removal window — an alias survives **at least one minor release** after its replacement ships before it can be removed (e.g. `REBAR_SYNC_PULL` ← deprecated alias `REBAR_NO_SYNC`). Scheduled aliases were dropped at the pre-1.0 breaking window (DE7): `REBAR_PUSH`, `TICKETS_TRACKER_DIR`, `REBAR_MCP_ALLOW_RECONCILE_LIVE`, and `verify.require_verdict_for_close` are no longer honored — use the canonical names. An unknown key is ignored with a typo warning, never a hard error. |
| **`rebar.llm` (optional `[agents]` extra)** | Opt-in and **less settled** than the core surfaces — the LLM review/verify/workflow framework ships under the optional `[agents]` extra. | Treat it as evolving pre-1.0; its structured verdicts (`review_result`, `completion_verdict`, `plan_review_verdict`) are covered by the `--output json` schema contract above, but the surrounding Python API may change more freely than the core facade. |

The **removal-window rule** applies to every "deprecate-then-remove" row above
(CLI options, config keys, MCP tools): a deprecated name is kept working for at
least one minor release after its replacement lands, and its removal is called out
in the changelog first.

## What "private" means

Anything named with a leading underscore — `rebar._*` modules/functions, the
on-disk store layout beyond the event schema, internal CLI helpers — is **not**
part of the public contract and may change without notice. Read the store through
the `rebar` CLI / library, not by parsing files directly (the on-disk form is
deliberately not human-readable).

## Related

- [docs/output-schemas.md](output-schemas.md) — the per-command `--output json` contract.
- [docs/event-schema.md](event-schema.md) — the event wire format and `SCHEMA_VERSION`.
- [`rebar.types`](../src/rebar/types.py) — the generated Python return types.
