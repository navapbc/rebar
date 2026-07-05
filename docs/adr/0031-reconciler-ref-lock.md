# ADR 0031 — Reconciler ref-lock: a bare-ref CAS lock for the pass-lock / phase-gate

**Status:** Accepted (epic 6c9c — dust-troth-naval / task 524d — pony-ditch-armor, C1)
**Date:** 2026-07-04
**Extended by:** C2 (lease + heartbeat + steal), C3 (reconciler cutover), C4 (retire the band-aids)

## Context

The Jira reconciler serializes its passes with an advisory **pass-lock** and gates phase
advancement with a **phase-gate**. Today both are *files* (`.reconciler-pass-lock`,
`.reconciler-phase-gate`) **committed to the `tickets` orphan branch**. Living in the tickets
working tree is the source of three chronic problems:

1. They collide with every other tickets-branch writer, so they need `merge=ours`
   `.gitattributes` entries to survive union merges — a band-aid that hides real conflicts.
2. A crashed pass-holder leaves the lock file committed with no self-healing path, so the
   `b859-8fa1` retry loop was bolted on to paper over the resulting contention.
3. Every lock read/write is a full detached-worktree commit + `update-ref` CAS on the busy
   tickets branch.

A lock is not ticket data. It should not live in the tickets tree at all.

## Decision

Move the lock off the tree and onto its **own git ref pointing directly at a blob**:
`refs/reconciler/lock` and `refs/reconciler/gate` (one schema, shared). A ref → blob is never in
any working tree, is never union-merged, and never touches the tickets branch. C1 delivers the
low-level primitive (`src/rebar/_engine/rebar_reconciler/_ref_lock.py`); no reconciler is wired to
it yet (that is C3).

### Blob schema

Newline-terminated UTF-8 JSON, stable key order:

```json
{"holder": "<pass id>", "lease_secs": 120, "heartbeat_ns": 173..., "fence": 0}
```

* `heartbeat_ns` — `time.time_ns()` at acquire/renew. **Diagnostic only.** The skew-proof
  lease-expiry rule (C2) reads `fence` + the ref oid, never this wall clock. We deliberately do
  **not** use `rebar._store.hlc` — that fcntl-guarded cache orders *ticket events*; a lock is a
  different concern and coupling them would be wrong.
* `fence` — a monotonic **progress-witness / generation counter**, seeded to `0` on acquire; C2
  increments it on renew/steal. It is the signal the expiry rule reads. It is **not** a full
  fencing token: no stale-writer rejection is claimed of the protected resource.

### CAS contract

`git hash-object -w --stdin` plants the blob and yields its OID; `git update-ref <ref> <oid> <old>`
then advances the ref **only if it still points at `<old>`**:

* **acquire** — create-only CAS, `<old>` = 40 zeros. A mismatch means the ref already exists →
  "lock already held" → **definitive, not retried** → `RefLockHeldError`.
* **release** — `git update-ref -d <ref> <old-oid>` against the exact observed OID. A mismatch
  (ref already gone, or owned by someone else after a steal) is a benign **idempotent success**
  (returns `False`, "nothing deleted"), never an error.

`git` reports a CAS old-sha mismatch as **exit 128** for the create/advance form and **exit 1**
for the delete form; both carry `cannot lock ref '<ref>'` in stderr. There is exactly **one** CAS
discriminator in the reconciler: `_advisory_lock._is_cas_mismatch(exc, ref_name=…)`, generalized
off its hard-coded `refs/heads/tickets` to take a ref name (default preserves every existing
tickets-branch call site). It accepts exit 128 (the historical tickets signal) **or** an exit-1
`cannot lock ref` (the ref-lock delete CAS) — a strict superset that never misclassifies an
unrelated failure.

### Three exit-128 outcomes, one classifier

acquire / release / (C2) steal each face a CAS mismatch that means something *different* — "held",
"idempotent success", "lost the single-winner race". Rather than three classifiers or a
parameterized retry policy, we factor a single-shot seam `_advisory_lock._cas_once(fn, ref) ->
bool` (`True` = CAS succeeded; `False` = CAS mismatch; re-raises any non-mismatch error). The
existing `_cas_advance_with_retry` becomes `_cas_once` in a retry loop (behaviour unchanged for the
tickets branch). Each ref-lock caller interprets the `False` return itself, so **only genuinely
transient CAS races are ever retried** — acquire/release/steal are single-shot with a definitive
verdict.

`_ref_lock.py` imports `_cas_once` / `_is_cas_mismatch` from its sibling `_advisory_lock.py` (via
the package's by-path loader). We deliberately did **not** extract a new `_git_cas.py` module: two
call sites is below the rule-of-three, C4 leaves these helpers in place, and a fresh <100-LOC file
would violate the module-size policy. The coupling is one internal reconciler module importing a
shared primitive from another — recorded here so it is a decision, not an accident.

### Distributed operation (local vs remote)

The lock must be authoritative across CI runners, manual invocations, and clones. `read` /
`acquire` / `release` take an optional `remote`:

* `remote is None` — a pure local `git update-ref` CAS (unit tests / single clone).
* `remote="origin"` — `read` force-fetches `+<ref>:<ref>` first (the remote is the truth), and the
  CAS is done as `git push --force-with-lease=<ref>:<old-oid>` (explicit lease-ref form, **never**
  bare) — the remote-side equivalent of the old-oid `update-ref` CAS. A rejected lease (`stale
  info` / `rejected` / `cannot lock ref`) is translated into the same exit-128 `update-ref` shape
  the shared discriminator understands, so there is still one CAS classifier.

The reconciler passes `remote="origin"` and the workflow gains a `refs/reconciler/*` fetch refspec
in **C3**. This complements — it does not replace — the `concurrency:` group in
`reconcile-bridge.yml`: that group only serializes runs *within one repo's workflow*; a
manually-invoked pass or a second clone bypasses it, and the ref lock is the cross-invocation guard.

### AC0 — remote-refspec feasibility

The whole design rests on a **non-branch `refs/reconciler/*` ref pointing at a blob** surviving a
push+fetch round-trip. Verified mechanically against a bare remote (see
`tests/unit/rebar_reconciler/state/test_ref_lock.py::test_ac0_blob_ref_roundtrips_through_remote`):
a blob-pointing `refs/reconciler/lock` pushes, is stored as a `blob` on the remote, and reads back
through a fresh clone; a racing create-only push loses the CAS.

**Working refspec (blob path):**

```
git push  --force-with-lease=refs/reconciler/lock:<old-oid> origin <blob-oid>:refs/reconciler/lock
git fetch origin +refs/reconciler/lock:refs/reconciler/lock
```

**GitHub note + live probe.** Standard git accepts blob-pointing refs under any writable namespace,
and `contents:write` is the scope that governs custom-namespace ref writes on GitHub. Because
hosted GitHub can special-case non-`refs/heads/*` / non-`refs/tags/*` namespaces, C3 wires a
one-shot CI probe (push+delete a scratch `refs/reconciler/*` ref with the CI `GITHUB_TOKEN`) so the
live acceptance is proven in the actual environment, not merely asserted here.

**Why ref → blob as the primary (not ref → tiny-commit)?** A blob is the minimal object: acquire
is one `hash-object` + one `update-ref`/push, with no tree or commit to build, and `read` is a
single `cat-file`. ref → tiny-commit adds a tree + commit object and an extra indirection
(`<ref>:lock.json`) per op for no functional gain — so it is the *fallback*, used only if a host
refuses blob-pointing refs, not the default.

**Object-GC safety.** A blob that a ref points at is *ref-reachable*, and standard git GC never
reaps a reachable object — so a held lock's blob is safe for as long as the ref exists (i.e. for the
lock's whole lifetime). The theoretical risk is a host that GCs objects reachable *only* via a
non-`refs/heads`/`refs/tags` namespace; the C3 live probe surfaces that, and the ref → tiny-commit
fallback (whose blob is tree-reachable from a commit) removes any doubt. Released locks delete the
ref, leaving the blob unreachable and collectable — the intended outcome.

**Fallback — ref → tiny-commit (fully specified).** If GitHub rejects a blob-pointing ref, the ref
points instead at a tiny commit whose tree holds a single `lock.json` with the *same* schema.
`read` selects the retrieval by `git cat-file -t <ref>`: `git cat-file blob <ref>` (blob case) vs
`git cat-file blob <ref>:lock.json` (commit case). `git hash-object -w` still plants the payload and
the create-only / observed-oid CAS is identical. `_ref_lock.read` already handles both object types,
so the fallback is a packaging change, not a protocol change.

### Fail-closed reads + timeouts

`read` raises `RefLockCorruptError` (a `ValueError`) on empty / non-UTF-8 / invalid-JSON /
missing-field / `fence` not a non-negative int / `lease_secs` not a positive number — carrying the
raw bytes — and callers treat that (indeed **any** read failure) as **HELD, never free**. Every git
subprocess is time-bounded: **5 s** for local object/ref ops, **30 s** for remote push/fetch; a
timeout raises `RefLockTimeoutError`, also treated as HELD.

### Operator break-glass

A wedged lock (before C2's lease self-healing exists) is cleared manually:

```
git push origin :refs/reconciler/lock      # remote
git update-ref -d refs/reconciler/lock     # local
```

C2's relative-duration lease makes this rarely necessary — a crashed holder's lock becomes
steal-able after one lease interval automatically.

## Consequences

* The lock leaves the tickets tree entirely: no `merge=ours` entry needed for it, no tickets-branch
  commit per lock op, no union-merge hazard. (Removing the now-dead `merge=ours` entry and the
  `b859` retry loop is C4, once the ref backend has baked behind C3's `lock_backend` switch.)
* One CAS discriminator, one single-shot seam — acquire/release/steal cannot accidentally retry a
  definitive outcome.
* C1 is intentionally incomplete as a *self-healing* lock: a crash wedges the ref until manual
  break-glass or C2's lease expiry. That is the C1/C2 seam, not a gap.
