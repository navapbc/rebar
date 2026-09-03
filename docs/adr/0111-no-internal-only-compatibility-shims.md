# ADR 0111 — No internal-only compatibility shims after private moves

**Status:** Accepted
**Date:** 2026-09-03

## Context

rebar now has enough split-module history that an old private import path can look like a
compatibility obligation. That is wrong for internal-only names. The public API stability
policy already says underscore-prefixed `rebar._*` modules and functions are private and may
change at any time, while public `rebar.*` facades, JSON/MCP schemas, CLI/config deprecation
surfaces, the event schema, and persisted-data migrations carry compatibility promises.

Private compatibility shims are actively hazardous in this codebase. Internal consumers include
ordinary imports, string-based dynamic imports, `getattr` lookups, module-qualified monkeypatch
targets, and test patch strings. Leaving an old private binding in place after a move makes it
easy for a consumer to bind to the old module while production code calls the new canonical site.
That split can turn tests into false positives and can hide the real call site from reviewers.

## RESEARCH findings

- `docs/api-stability.md` defines the compatibility boundary: public `rebar.*` functions and
  typed returns, structured output schemas, MCP schemas, event schema, CLI/config deprecation
  surfaces, and persisted data are compatibility-bearing. Leading-underscore modules/functions
  and internal helpers are explicitly private.
- [ADR 0083](0083-reconciler-vendor-adapter-seam.md) already recorded the key failure mode for
  private module moves: module-qualified monkeypatches bind to the named module, not to the call
  site production code uses. Its vendor-adapter migration therefore required exactly one binding
  site and atomic migration of import and patch strings.
- [ADR 0016](0016-project-det-invariants.md) retained `run_security_detectors` as a thin
  deprecated alias after renaming it to `run_detectors`. That was a historical pre-policy private
  alias, not a pattern to copy.
- [ADR 0092](0092-bridge-primary-vocabulary-compatibility-adapters.md) intentionally kept
  reconcile compatibility adapters because they preserve documented public/operator behavior.
  Those adapters are outside this policy's prohibition.
- `docs/architecture.md` records several historical split decisions that retained internal
  re-exports or shims to keep old private paths working. Those notes remain useful history, but
  without a governing policy they read as future guidance.

## Decision

After a private symbol or private module moves, there is exactly one canonical binding for that
name. The old private binding is deleted in the same change that introduces the new one.

A private move must migrate every internal consumer atomically to the canonical path, including:

1. source imports and attribute access;
2. tests;
3. string lookups used by `getattr`, registries, or route tables;
4. dynamic imports and file-location loaders; and
5. module-qualified monkeypatch or mock targets.

A shorthand for this obligation is: source, test, string, dynamic import, and monkeypatch
consumers move together.

A change is not complete while any internal-only shim, re-export, deprecated alias, or forwarding
wrapper exists solely to preserve an old private path. If the migration cannot be made atomically,
the work must be split so the canonical binding and all consumers move together within a safe
slice, not padded with a temporary internal compatibility layer.

This policy does not remove or weaken independently specified compatibility contracts. Valid
exceptions are:

- Public facades and documented `rebar.*` names, including public Python facades;
- CLI/operator deprecation surfaces and config-key aliases;
- MCP tool names and MCP input/output schemas;
- JSON output schemas and generated public return types;
- event readers and append-only event schema compatibility; and
- persisted-data migrations and readers for historical on-disk data.

Those exceptions must be justified by the public/operator/wire/data contract they protect, not by
convenience for private source or tests.

## Research critique and maintainer/operator resolutions

1. **ADR 0016 retained a private deprecated alias.** `run_security_detectors` survived as a thin
   alias to `run_detectors`, and a test asserted equivalence. That contradicts the rule this ADR
   now adopts for future private moves. **Resolution:** treat ADR 0016's alias as a pre-policy
   exception and a migration target when that area is next touched; do not cite it as precedent for
   new private aliases.
2. **ADR 0083's no-re-export rule was local to the reconciler adapter seam.** The monkeypatch
   analysis was correct but scoped to physically moved reconciler modules. **Resolution:** promote
   the same reasoning to a repository-wide policy for private moves: one canonical binding, old
   binding deleted, all import/patch strings migrated atomically.
3. **ADR 0092 keeps compatibility adapters.** Read literally beside this ADR, that could appear to
   permit any adapter after a rename. **Resolution:** ADR 0092 remains valid because it preserves
   public/operator reconcile behavior with documented defaults; it is an explicit public
   compatibility-contract exception, not an internal-only shim pattern.
4. **Prior architecture split notes celebrate internal re-exports.** Historical sections describe
   `push.py`, prompt, reads, and differ re-exports as split-enabling techniques. **Resolution:** keep
   those notes as history, but subordinate future work to this ADR. Any future private move must
   either delete the old binding in the same slice or record a separate public/wire/data contract
   that makes the adapter an exception.

The maintainer/operator resolution recorded on parent ticket `6640` is therefore: after a private
symbol/module moves, rebar keeps exactly one canonical binding; public facades, MCP wire schemas,
event readers, and persisted-data migrations remain separate compatibility contracts.

## Recurrence prevention evidence

This recurrence prevention evidence is intentionally durable rather than a new broad CI
mechanism:

- Review scope-intent for private moves must name the canonical target and state that old private
  import paths, string lookups, dynamic imports, and monkeypatch targets were searched and migrated.
- Code review should treat an internal-only forwarding wrapper or re-export as a scope violation
  unless the change identifies one of the public/wire/data exceptions above.
- The documentation checks in `tests/scripts/test_internal_shim_policy_docs.py` pin this policy in
  the ADR, API-stability page, and architecture guide so future edits cannot silently remove the
  governing rule.
- Existing ADR-number checks and the generated ADR index keep this decision discoverable. No new CI
  mechanism is introduced; the evidence rides the existing pytest and ADR-index checks.

## Consequences

- Private refactors have a sharper migration burden: the moved binding and all internal consumers
  move in one atomic slice.
- Tests become better evidence because monkeypatch targets and dynamic import strings point at the
  same canonical binding production code uses.
- Public compatibility remains explicit and intentional rather than inferred from private shims.
- Historical internal shims are not automatically deleted by this ADR, but new ones are disallowed
  and old ones should be removed opportunistically when their areas are already being migrated.
