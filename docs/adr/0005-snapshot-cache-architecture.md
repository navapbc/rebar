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

**The key must have REUSE — an immutable key is not automatically a good one.** "No staleness"
makes an immutable SHA *safe* to cache under; it does not make the cache *bounded*. That
follows only when the same key is requested again, which is why this decision was taken for
`origin/main`, a ref that moves per merge. A key that moves faster than it is queried never
hits, and the store degenerates into an append-only log of full copies — measured on the
`tickets` key at 64,483 entries / 47.2 GiB before it was corrected (bug `8386-a512-4815-4e6b`).

Two entry kinds live here and they satisfy this differently:

* **Code entries (`<sha>`)** satisfy it by *key reuse*: `origin/main` is stable for hours and
  concurrent gates collapse onto one SHA, so the hit rate carries the bound.
* **Ticket entries (`tickets-<sha>`)** cannot: the pin tracks the live tracker `HEAD`, which
  advances every ~26s while each commit touches a handful of files, so the key is effectively
  never requested twice. They satisfy the bound by *content reuse instead* — a new entry is
  built by hardlinking a verified neighbouring entry and rewriting only the paths that differ
  (`git diff --name-status` between the two SHAs). Adjacent SHAs therefore cost one delta, not
  one tree.

**Sharing changes what the byte total may be built from.** Once entries share inodes, "the
size of an entry" splits into three different questions, and using one answer for all three is
a defect: `entry_size` (plain `st_size`, double-counts a shared blob) is for REPORTING only;
`exclusive_size` (files with `st_nlink == 1`) is the increment for BOTH ends of the running
total, because populating adds exactly the bytes whose first link it created and evicting frees
exactly the bytes whose last link it removed; and `distinct_bytes` (each inode once) is the
ground truth the startup sweep reconciles to. Each inode is therefore added once and subtracted
once, so the incremental total tracks the authoritative walk exactly.

An apportioned `st_size // st_nlink` was tried and rejected. It is a fair way to divide bytes
for a report, but as an increment it is wrong in the direction that matters: evicting one of
`k` links credits a `1/k` share of bytes that are still on disk, so D5's free-space loop stops
short of its watermark and the `max_bytes` cap evicts against space it never recovered.

Sharing does not weaken D1's faithfulness guarantee: each entry still contains exactly the
committed tree at its own SHA. `git checkout-index --force` unlinks and recreates rather than
writing in place, and the delta path additionally unlinks every path it is about to rewrite, so
a published entry is never mutated through a shared inode. The delta path is fail-closed — no
donor, unresolvable objects, a donor whose file set does not match `git ls-tree -r` for its own
SHA, or an un-hardlinkable filesystem all fall back to the full materialization.

**Immutability binds READERS too — nothing writes inside a published entry.** The premise
above ("a published entry is never mutated") was violated in practice by the reducer, which
published its derived `.cache.json` inside every ticket dir it read through a pinned root —
measured at 4,844 post-materialization files in one live entry (bug `5c27-7926`). Post-read
writes break the janitor's TOFU reverify digest (a clean entry looks corrupt and is evicted)
and, once entries hardlink-share blobs, any in-place write through a shared inode corrupts
every entry linking it. Two guards enforce the premise: writers of derived state consult
`repo_snapshot.in_snapshot_entry` and skip the write under a store entry (the reducer cache
is a rebuildable optimization, so a pinned-root read simply stays uncached), and
`_store/fsutil.atomic_write`'s rename-over publication (`os.replace`, never in-place) is
pinned by test so an accidental write through a shared inode still cannot corrupt the
sibling entries.

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

**Amendment (bug `undamaged-epidermic-kakarikis`, 58a3-0756-e470-4b40): the pass needs a
per-host driver, not only the review-bot's.** As accepted, the "single background pass" had
exactly one production driver — the review-bot FastAPI lifespan — so every OTHER host that
resolves attested gates populated its store append-only (measured: 64,021 entries / 47.24 GiB
on one developer host). The cadence floor is now an **operation-linked trigger**
(`rebar._snapshot.gc_trigger`, mirroring the compaction trigger that fixed bug `0d15-59a4`):
the tail of an attested gate resolution performs one O(1) `stat` of a
`<root>/gc/last-pass.stamp` sidecar against the janitor `interval_seconds`, and when due
spawns a **detached child** that runs the same `run_gc` pass. "Never invoked from
populate/read" holds in substance: the reclamation work itself still never runs in the gate's
process or on any hot path — the in-line cost is one marker `stat` once per gate operation,
no store enumeration, and no ticket-store lock. Single-flight adds a stamped
`<root>/gc/worker.lock` sidecar (the shared v2 owner-stamp staleness table) in front of the
existing gc `flock`; a pass that stood aside (`skipped="locked"`) does not reset the stamp.
The review-bot's resident thread remains as a supplementary cadence; overlap degrades to
`skipped="locked"` by D5's own interlock.

### D6 — Attested signing binds the SHA via the EXISTING manifest channel

(Implemented in sibling story S4; recorded here for the cross-cutting picture.) The signed
verdict pins `verified_at_sha` as a `verified-at-sha:<sha>` MANIFEST STEP — it enters the
signed bytes without touching `signing._canonical_payload` or bumping `PAYLOAD_VERSION`, so
NO prior certified closure is invalidated. **Rejected alternative — a new signed-payload
field** for the SHA: it would bump `PAYLOAD_VERSION` and invalidate every existing
signature. The pin is shaped as an in-toto-style statement so a future move to
DSSE/asymmetric/transparency-log is an envelope swap, not a rewrite.

### D7 — Surfaces: a pinned one-to-one CLI↔MCP mapping + cwd/branch decoupling (S5)

The `ref`/`source` controls are exposed on BOTH the CLI and the MCP tools over exactly the
five code-reading operations, pinned one-to-one so the two surfaces never drift:

| CLI command            | MCP tool            |
|------------------------|---------------------|
| `rebar review-plan`    | `review_plan`       |
| `rebar verify-completion` | `verify_completion` |
| `rebar review` (review-ticket) | `review_ticket` |
| `rebar review-code`    | `review_code`       |
| `rebar scan-spec`      | `scan_spec`         |

`--source`/`source` is `{attested|local}` (default `attested`); `--ref`/`ref` defaults to
`origin/main` (review-code: the reviewed `head`), configurable via `REBAR_GATE_REF` /
`REBAR_GATE_SOURCE` / `[snapshot]`. **cwd/branch decoupling (recorded so it is not
re-coupled):** the verified-code path does NOT depend on the MCP server's checked-out branch —
in attested mode `REBAR_ROOT` only locates the object DB to fetch from, and the snapshot at the
pinned SHA is the read root. Future maintainers must keep both surfaces in lockstep over these
five ops and must not re-root the verified-code path back onto the server's working tree.

## Consequences

- Code-reading gates verify a client-pinned, immutable, reproducible snapshot — never the
  server's mutable checkout — safely under concurrent distributed use.
- The cache is regenerable/ephemeral (self-healing, reclaimable), not authoritative data;
  losing it costs only re-materialization.
- `source=local` remains the documented back-out to the prior in-place read (never signs).
- New operational requirement: the server must be able to `fetch` from origin (credentials
  for private repos). Failures surface as descriptive, actionable, fail-closed errors.
