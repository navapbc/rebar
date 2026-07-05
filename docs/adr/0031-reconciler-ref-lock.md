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

### AC0 — remote-refspec feasibility (PROVEN against GitHub origin)

The whole design rests on a **non-branch `refs/reconciler/*` ref pointing at a blob** surviving a
push+fetch round-trip. This was proven **live against the real `origin` GitHub remote**
(`github.com:navapbc/rebar`) on 2026-07-04: a blob-pointing scratch ref pushed, was advertised by
`git ls-remote` at exactly the blob OID, fetched back with its payload byte-for-byte intact, and
deleted — GitHub accepts blob-pointing refs under a custom `refs/reconciler/*` namespace, so the
ref→blob primary path is used and the ref→commit fallback (below) is **not** needed.

**Proving command (repeatable):** `scripts/prove_ref_lock_ac0.sh [<remote>]` (default `origin`)
runs the push → ls-remote → fetch → delete round-trip and exits non-zero if a host rejects the
blob-ref. It is the CI/maintainer artifact AC0 asks for; run it with the CI `GITHUB_TOKEN`
(`contents:write`). The same round-trip is covered by two tests: the hermetic
`tests/unit/rebar_reconciler/state/test_ref_lock.py::test_ac0_blob_ref_roundtrips_through_remote`
(bare remote, in the default suite) and the external-tier
`tests/external/test_ref_lock_ac0_live.py` (runs against the real `origin` under
`REBAR_RUN_EXTERNAL=1` — this is what was executed to earn the proof above).

**Working refspec (blob path):**

```
git push  --force-with-lease=refs/reconciler/lock:<old-oid> origin <blob-oid>:refs/reconciler/lock
git fetch origin +refs/reconciler/lock:refs/reconciler/lock
```

`contents:write` is the token scope that governs these custom-namespace ref writes on GitHub; the
proving script re-verifies acceptance in any environment where the write path might differ.

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

## Lease self-healing (C2 — extends this module)

C1 leaves a crashed holder's ref wedged until manual break-glass. C2 adds a **relative-duration,
skew-proof lease** (the DynamoDB-lock-client pattern) so a dead holder's lock becomes steal-able
after one lease interval — with **no cross-clone clock comparison**.

* **`renew(repo_root, ref, *, oid, remote=None) -> str`** — the heartbeat. Owner-only: it reads the
  current state and, only if the ref still points at the held `oid`, CAS-updates the blob to
  `{holder, lease_secs (unchanged), heartbeat_ns=now, fence+1}` against `oid`. If the ref is absent /
  moved, or the CAS is rejected, it raises **`LeaseLostError`** — it never silently retries into
  another holder's lock. Returns the new oid the caller threads into its next `renew`.
* **Expiry rule (skew-proof).** A contender records `(oid, fence)`, waits **one lease measured on its
  own clock**, and re-reads; the holder is considered dead **only if neither the ref oid nor `fence`
  advanced** over that full lease. `heartbeat_ns` is never compared across clones — it is diagnostic;
  `fence` is the progress witness. Because the wait is one *lease* and the holder heartbeats every
  `heartbeat_interval = max(1, lease // 3)` seconds, a live holder advances `fence` ≥3× per lease and
  is never mistaken for dead.
* **CAS-break-on-stale (single-winner).** The steal is split so it is testable without real time:
  `try_break_if_stale(…, first, holder)` re-reads and, if `(oid, fence)` is unchanged, CAS-replaces
  the blob against `first.oid` (new holder, `fence = first.fence + 1`); a rejected CAS means another
  contender won — it returns `None`, it does **not** also acquire. The convenience `steal(…, holder,
  sleep_fn=time.sleep)` does observe → `sleep_fn(first.lease_secs)` → break. A **free** ref is never
  "stolen" (that is `acquire`); a **live** holder is never stolen (regression-tested); a corrupt /
  unreadable blob fails closed (never stolen).
* **Lease config.** `DEFAULT_LEASE_SECS = 120`; the value is carried IN the blob so a contender waits
  the *holder's* lease, not its own. C3 wires `[reconciler] lock_lease_secs` into the `acquire` call.
* Structured logs fire on lease-steal (won), expiry-detection (no progress over one lease), and
  renewal-failure (`LeaseLostError`).

## Consequences

* The lock leaves the tickets tree entirely: no `merge=ours` entry needed for it, no tickets-branch
  commit per lock op, no union-merge hazard. (Removing the now-dead `merge=ours` entry and the
  `b859` retry loop is C4, once the ref backend has baked behind C3's `lock_backend` switch.)
* One CAS discriminator, one single-shot seam — acquire/release/steal cannot accidentally retry a
  definitive outcome.
* C1 is intentionally incomplete as a *self-healing* lock: a crash wedges the ref until manual
  break-glass or C2's lease expiry. That is the C1/C2 seam, not a gap.
