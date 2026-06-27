# ADR 0005 — Content-addressed snapshot cache + janitor architecture

Status: Accepted
Date: 2026-06-26
Epic: `raze-vet-ditch` (Repo-snapshot isolation for code-reading gates)

## Context

The rebar MCP server is a long-lived process pinned to ONE working directory. Every
code-reading gate (`review_plan`, `verify_completion`, `review_ticket`, `review_code`,
`scan_spec`) used to read PROJECT SOURCE from that mutable, shared checkout at call time.
A parallel task that switched the shared branch produced a FALSE-NEGATIVE completion
verdict on work correctly merged to `main`, and an HMAC-signed verdict computed against a
moving branch is not reproducible. We need every gate to read a *faithful, immutable,
reproducible* tree at a client-pinned SHA, safely under concurrent distributed use.

Prior art surveyed: Gitaly / Sourcegraph gitserver+zoekt (server-side git materialization),
Bazel / ccache / Nix (content-addressed caches; move GC off the hot path), the GitHub
tarball API (faithful tree export), in-toto (signed attestation envelopes).

## Decisions

### D1 — Faithful materialization via `read-tree` + `checkout-index`, NOT `git archive`

`git archive` is lossy as an attestation basis: it drops `.gitattributes export-ignore`
paths, applies `export-subst`, omits submodule contents, and emits Git-LFS pointer text.
We materialize the committed tree with `git read-tree <sha>` into a *throwaway* index
(`GIT_INDEX_FILE`) followed by `git checkout-index --all --prefix=<tmp>/`. This reproduces
the committed blob for every entry (export-ignore files present; export-subst NOT applied —
committed bytes verbatim) and, because the index is a throwaway file, never touches the
repo's own `index.lock`/working tree, so different-SHA materializations never contend.

Faithfulness limits are DETECTED and surfaced (never silently wrong): LFS-tracked paths
materialize as their committed pointer text (detected by magic header, recorded on the
handle); submodule gitlinks (mode 160000) have no blob and are omitted (recorded on the
handle). Rejected alternative: `git worktree add` per ref — takes repo-level index/config
locks (the exact contention we avoid) and still needs a checkout to yield a faithful tree.

### D2 — Content-addressed cache layout

An immutable SHA is a perfect cache key (no staleness), so entries live at `<root>/<sha>/`,
outside the repo (under `REBAR_GATE_TMPDIR` or the system temp dir — never a hardcoded
`/tmp`). Population is atomic: build under `<root>/tmp/<uuid>/`, fsync, `rename` into
`<root>/<sha>` — a reader never observes a partial tree. Single-flight (an in-process
per-SHA lock + a cross-process `flock` on `locks/<sha>.lock`) collapses concurrent same-SHA
requests to one materialization; a lost race is merely wasteful (same SHA == same content),
never wrong. A running byte total is maintained incrementally (atomic flock read-modify-
write) so the janitor never needs a hot-path `du`.

### D3 — Reader safety via POSIX delete-on-last-close (and the REJECTED PID lease)

Readers open files up front; eviction renames an entry to `trash/<uuid>` (atomic
disappearance) THEN `rmtree`s it — NEVER an in-place recursive delete of a live entry — so a
reader holding an open fd keeps reading the evicted content (POSIX delete-on-last-close),
and a *new* lookup that hits `ENOENT`/a read error treats it as a miss and re-materializes.

**Rejected alternative — a PID + heartbeat reader lease.** A spike showed it is unsound:
an entry has N concurrent readers (one lease slot can't model them), PIDs are reused (a
stale lease points at an unrelated process), and a crashed reader leaves the lease held
forever (crash-stale). Mature systems (Gitaly, Sourcegraph, Bazel, ccache) rely on kernel
guarantees instead, which is what delete-on-last-close gives us. There is deliberately NO
PID/heartbeat lease anywhere in the cache or janitor.

### D4 — Recency by touch-on-read `mtime`, never `atime`

The janitor evicts LRU by `mtime`, which the cache bumps explicitly on every hit. `atime`
is unreliable (kernels mount `relatime`/`noatime`), so it is never used as the recency
signal.

### D5 — Janitor: off the hot path, flock-interlocked, recoverable

A single background pass (never invoked from populate/read) reclaims under a free-space
watermark (LRU by `mtime`, skipping a short grace window), backstopped by the byte total
and a secondary max-age cold-trim. A pass holds an exclusive `flock` on `<root>/gc/lock`
(a second process's pass cannot overlap); population stays lock-free. Startup sweep clears
`tmp/*` + `trash/*` and reconciles the byte total via one authoritative full walk; an
interrupted rename→rmtree straggler is re-drained on a later pass. A corrupt/truncated entry
is detected by a content-digest reverify and discarded so the next acquire re-materializes.
All thresholds (watermark, grace, max-age, reverify period, interval) are configurable with
documented defaults (`REBAR_GATE_*` env > `[snapshot]` config > default).

### D6 — Attested signing binds the SHA via the EXISTING manifest channel

(Implemented in sibling story S4; recorded here for the cross-cutting picture.) The signed
verdict pins `verified_at_sha` as a `verified-at-sha:<sha>` MANIFEST STEP — it enters the
signed bytes without touching `signing._canonical_payload` or bumping `PAYLOAD_VERSION`, so
NO prior certified closure is invalidated. **Rejected alternative — a new signed-payload
field** for the SHA: it would bump `PAYLOAD_VERSION` and invalidate every existing
signature. The pin is shaped as an in-toto-style statement so a future move to
DSSE/asymmetric/transparency-log is an envelope swap, not a rewrite.

## Consequences

- Code-reading gates verify a client-pinned, immutable, reproducible snapshot — never the
  server's mutable checkout — safely under concurrent distributed use.
- The cache is regenerable/ephemeral (self-healing, reclaimable), not authoritative data;
  losing it costs only re-materialization.
- `source=local` remains the documented back-out to the prior in-place read (never signs).
- New operational requirement: the server must be able to `fetch` from origin (credentials
  for private repos). Failures surface as descriptive, actionable, fail-closed errors.
