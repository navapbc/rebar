# ADR 0092 — Bridge-primary vocabulary with explicit reconcile compatibility adapters

> **Amended (2026-09-04) — see §Amendment.** The compatibility adapters this ADR kept were removed by ticket `82bb-439b-1c42-4abd`; only the engine-only `--filter-local-ids` post-filter survives.

> **Archived (2026-09-07).** Consolidated into [ADR 0121](0121-reconciler-architecture-consolidated-record.md) §19; kept verbatim as the evidence record.

**Status:** Archived — consolidated into [ADR 0121](0121-reconciler-architecture-consolidated-record.md) (was: Accepted)
**Date:** 2026-08-08

## Context

rebar historically exposed Jira synchronization through several surfaces with different
defaults. `rebar reconcile` defaults to dry-run, direct argument-less engine invocation
defaults to live, and the library and MCP reconcile operations have their own established
guards. The legacy `--filter-local-ids` option filters writes after the full differ, while
canonical ticket selection must narrow examination itself. Reconcile-check is a lock-free
diagnostic that writes its own report and is not equivalent to an ordinary preview.

A direct alias from every old spelling to `bridge sync` would therefore change observable
behavior and break checked-out workflows and environments.

## Decision

Make `rebar bridge preview` and `rebar bridge sync` the primary operator vocabulary. Both
engine subcommands and retained legacy flags normalize into one internal request before the
existing pause, phase, lock, pass, and exit-policy spine.

Keep explicit compatibility adapters for `rebar reconcile`, direct engine `--mode`, the
library reconcile function, the MCP reconcile tool, and `--filter-local-ids`. Their historical
defaults and write-filter semantics remain unchanged. Reconcile-check remains a real `Mode`
member and retains its dedicated lock-free route.

Canonical `bridge preview` is also lock-free, but it is not a spelling alias for
reconcile-check: it runs the ordinary differ once and returns a deterministic manifest of the
proposed field changes without acquiring or mutating the writer locks. Canonical preview and
sync retain comparable manifest envelopes (route, mode, counts, and field-level plan) so an
operator can compare the previewed work with the subsequent sync result.

Canonical `--only` and `--except` resolve every local ID or bound Jira key read-only before
lock inspection, then narrow local, previous-remote, and current-remote differ inputs. A
partially unresolved or subsequently vanished selection fails closed without differ or apply.

Canonical `bridge sync --max-changes N` remains `Mode.LIVE`. It uses the existing deterministic
mutation ordering and retains a manifest containing `max_changes` and the full deferred list,
even when the ceiling exceeds the workset. Canonical uncapped `bridge sync` also retains its
comparable manifest. The legacy uncapped LIVE route still removes its temporary manifest and
returns its historical tally; legacy bootstrap modes and `MODE_CAPS` remain unchanged.

## Consequences

- Current workflows, scripts, help, and runbooks use bridge vocabulary.
- Older workflow revisions and external consumers continue to work unchanged.
- Canonical selection is intentionally stronger than the legacy write filter.
- Canonical preview never waits on or changes writer locks, and canonical preview/sync output is
  field-comparable without changing the legacy manifest contract.
- Removing compatibility requires a separate deprecation decision after real consumers have
  migrated; this decision does not authorize removal.

Rollback is additive: callers can return to the retained `rebar reconcile --mode ...` adapter
without changing the shared reconciliation core or persisted store data.

## Amendment (2026-09-04) — the compatibility adapters were removed; only `--filter-local-ids` survives

Ticket `82bb-439b-1c42-4abd` (seagreen-wet-bluefish) removed, as a pre-1.0 BREAKING change, the top-level CLI
`rebar reconcile` route, direct-engine `--mode reconcile-check`, the Python `rebar.reconcile` facade, the MCP
`reconcile` tool and the persisted `reconcile_diagnostics` status field (`docs/release-notes.md`
§"legacy reconcile compatibility surfaces are removed"). The Decision's "Keep explicit compatibility
adapters" paragraph no longer describes the tree: `rebar bridge preview|sync|fsck|status` and their
library/MCP twins are the only vocabulary. One adapter survives — the direct-engine legacy route's
`--filter-local-ids` engine-only post-filter, restored by commit `e8fbf305057b` for live-DC harness scoping
that `--only` cannot express for not-yet-bound inbound Jira keys.
[ADR 0111](0111-no-internal-only-compatibility-shims.md) cites this ADR as its public compatibility-contract
carve-out; that carve-out now covers exactly that one flag. The Consequences bullet "Removing compatibility
requires a separate deprecation decision" was satisfied by the pre-1.0 removal pass recorded on that ticket.
