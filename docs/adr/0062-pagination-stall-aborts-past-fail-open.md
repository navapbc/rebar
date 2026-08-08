# ADR 0062 — A stalled pager must abort PAST fail-open handlers

- **Status:** Accepted (epic `b5bc-f4f7-3433-4181`; ticket `9ce8-8ff1-0832-4825`)
- **Date:** 2026-08-07

## Context

The reconciler's whole-project reads (parent maps, issuelink maps, comment maps, the base
snapshot search) are paged. Each enrichment read is wrapped, by design, in a **fail-open**
handler: a transient failure logs a WARNING, returns an empty/degraded result, and the pass
continues rather than aborting the whole reconcile. That degradation contract is deliberate and
correct — *for transient faults*.

A **pagination stall** is not a transient fault. A pager can prove the server has stopped
honouring its paging parameter — an offset pager whose `startAt` is ignored (the same page
comes back at a new offset), or a cursor walk handed the same non-null `nextPageToken` twice in
a row. Every further request would return the same page forever, so the read is **truncated**,
not failed. If a fail-open handler degrades around a truncated whole-project read, it writes a
snapshot the differ then treats as **authoritative**:

- a missing parent reads as *parentless*,
- a missing issuelink map reads as *"no links"* (so link removals become undetectable),
- a missing comment map reads as *no comments*.

That is **silent data loss** wearing the costume of a successful pass.

**This exact class has shipped repeatedly here — three measured incidents:**

- **bug `deac`** — `fetcher._iter_pages` (the base snapshot pager).
- **bug `9263`** — the Jira Data Center transport pager (`_paged_search`).
- **bug `cabc-7a98-d173-4d7c`** — Cloud's `acli_graph` `nextPageToken` cursor walk.

A related fourth defect, **bug `9a46`**, was the *mechanism* by which one of these stayed
silent: Cloud's `RunawayPaginationError` originally derived from `RuntimeError` directly rather
than from the neutral stall class, so core's re-raise clauses (which name only
`BackendPaginationStallError`) missed it and the stall was swallowed into a degraded snapshot.

## Decision

**A proven pagination stall is raised as `BackendPaginationStallError` and every reader
re-raises it PAST its fail-open handler.** The loud abort is the *contract*, not a per-reader
judgement call: a reader that catches broadly (`except Exception`) to fail open must first
`except BackendPaginationStallError: raise`.

Two structural consequences follow and are load-bearing:

1. **The stall error lives in core** (`_backend.py`, beside the other `Backend*` errors), so
   BOTH the adapters that raise it and the core `fetcher` handlers that must re-raise it can
   name one type. Core must never import `adapters/`, so an adapter-local error would be
   unnameable at exactly the boundary that absorbs it.
2. **Every vendor-specific stall type subclasses `BackendPaginationStallError`** (e.g. Cloud's
   `RunawayPaginationError`), so the single core re-raise clause catches all of them. Deriving
   a stall type from `RuntimeError` directly re-opens bug `9a46`.

## Consequences

- A stalled whole-project read aborts the pass loudly instead of persisting a truncated
  snapshot as authoritative — the failure the three incidents share is now structurally
  prevented rather than re-discovered per reader.
- Fail-open handlers keep their intended scope (transient faults) and are **not** weakened: the
  narrow `except BackendPaginationStallError: raise` sits *above* the broad handler, so only the
  provable-stall case escapes.
- New enrichment readers inherit the obligation: any `except Exception` fail-open path over a
  paged read must re-raise the stall type first, or it re-introduces the silent-loss class.

## Rejected alternatives

- **Let each reader decide whether a stall is fatal.** That is precisely the per-reader choice
  that produced three separate incidents; the contract exists to remove the choice.
- **Degrade around a stall but flag the snapshot as partial.** The differ treats absence as
  authoritative by construction; a "partial" flag every downstream consumer must remember to
  honour is the same silent-loss risk relocated, not removed.
