# ADR 0092 — Bridge-primary vocabulary with explicit reconcile compatibility adapters

**Status:** Accepted
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

Canonical `--only` and `--except` resolve every local ID or bound Jira key read-only before
lock inspection, then narrow local, previous-remote, and current-remote differ inputs. A
partially unresolved or subsequently vanished selection fails closed without differ or apply.

Canonical `bridge sync --max-changes N` remains `Mode.LIVE`. It uses the existing deterministic
mutation ordering and retains a manifest containing `max_changes` and the full deferred list,
even when the ceiling exceeds the workset. Uncapped LIVE still removes its temporary manifest;
legacy bootstrap modes and `MODE_CAPS` remain unchanged.

## Consequences

- Current workflows, scripts, help, and runbooks use bridge vocabulary.
- Older workflow revisions and external consumers continue to work unchanged.
- Canonical selection is intentionally stronger than the legacy write filter.
- Removing compatibility requires a separate deprecation decision after real consumers have
  migrated; this decision does not authorize removal.

Rollback is additive: callers can return to the retained `rebar reconcile --mode ...` adapter
without changing the shared reconciliation core or persisted store data.
