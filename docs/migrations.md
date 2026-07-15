# Migrations — the idempotent ensure-registry (School B)

rebar keeps an **already-initialized** store converged with the current binary through a
first-class, idempotent **ensure-registry** (`rebar._store.ensures`). This is the reuse
guide: how to add a unit, where it runs, the markers and the write-path nudge, the accepted
trade-offs, and the future A-tier ledger.

## Two schools (and which one this is)

- **School B — desired-state / convergent (what rebar does).** Each unit is *check-then-act*:
  it inspects current state and acts only if drift is present, so it is safe to re-run any
  number of times (Ansible/Puppet/Chef `changed`/`ok`; Kubernetes level-triggered reconcile).
  There is **no ordered version ledger** — units are independent and self-checking.
- **School A — ordered ledger (NOT built; future work).** An ordered, once-only, unsafe-to-
  re-run migration sequence with a committed store-format version (Alembic/Flyway/Rails). See
  [A-tier ledger (future)](#a-tier-ledger-future) below.

The ensure-registry generalizes the old init-time `_migrate_*`/`_ensure_*` steps, which ran
**only** at `init`/re-init — so a fix shipped *after* a store was initialized never reached it
(the `gc.auto=0` legacy-store gap). Now those steps are ensure units that converge every
existing store on any init/re-init, `fsck --repair`, or MCP boot.

## Anatomy: `EnsureOutcome` + the registry

Each unit has a **stable, immutable id** and a callable `(tracker) -> EnsureOutcome`:

```python
@dataclass(frozen=True)
class EnsureOutcome:
    id: str
    status: Literal["ok", "changed", "failed"]  # ok = already converged (no-op);
    detail: str = ""                             # changed = drift corrected; failed = raised
```

`run_ensures(tracker)` runs **every** unit unconditionally under the store write lock, catches
a raising unit (skip-and-continue → `failed`, excluded from the applied-set), and returns the
outcomes. It never raises: a write-lock acquisition failure or a marker-write error is logged
and treated as a whole-sweep no-op, so init / boot never abort on ensure trouble.

## Adding an ensure unit

1. Implement a **check-then-act** callable that returns an `EnsureOutcome`. Read current state
   first; act (and return `changed`) only on drift; otherwise return `ok`. Let exceptions
   propagate — `run_ensures` converts a raise into `failed` and keeps going. Commit units
   (e.g. `.gitattributes`) must **tree-check before committing** so a converged store makes
   **zero** git commits on the next sweep.
2. Give it a **stable id** and register it in `rebar._store.ensures`: add the id to the frozen
   `REGISTRY_IDS` tuple **and** map it to the callable in `_registry()`. A test asserts the two
   agree, so a rename/typo can't silently strand a unit as forever-pending (or let an applied
   unit reappear as pending). **Never reuse or repurpose an id** — it is persisted in the
   applied-set marker.
3. That's it — the unit now runs at every entry point below and is surfaced by `fsck`.

The six built-in units: `env-id`, `gc-config`, `merge-ours`, `gitattributes`, `gitignore`,
`store-compat`.

## Where `run_ensures` runs

- **`init` / re-init** and the **symlink worktree attach** (`init.py`) — the sweep replaces the
  old hand-listed `_migrate_*`/`_ensure_*` calls; init logs any `failed` unit and never aborts.
- **`rebar fsck --repair`** — the ensure-sweep is folded into the existing "drive healthy"
  verb as a distinct phase (no new mutation flag). `--dry-run` does not sweep.
- **MCP server startup** (`rebar-mcp`) — best-effort, after the `--help` check and before the
  server runs, with a **short** write-lock budget so a contended lock skips rather than delays
  boot. A missing store / import / sweep error never aborts boot.

Plain read-only `rebar fsck` prints an informational `ensures: N/M applied` line (N = applied
units present in the marker ∩ registry, M = registry size) **without** sweeping; it is
text-only (excluded from `--output json`, so it never inflates `issue_count`).

## The two markers (both git-ignored)

- **`.ensure-applied`** — a JSON array of the **non-failed** unit ids from the last sweep,
  written atomically (`fsutil.atomic_write`, temp-in-same-dir + rename). Absent / garbage /
  non-list degrades to the **empty set** (a pre-feature or corrupt store reads as "everything
  pending"). It is a **hint**, not a gate: units are always re-run and self-check regardless.
- **`.ensure-hinted`** — a single last-hinted timestamp that rate-limits the write-path nudge.
  Absent / unparseable ⇒ "never hinted".

## The write-path pending-hint (Rails CheckPending, hardened)

On a covered write, `maybe_emit_pending_hint(tracker)` nudges when the store is behind:

- **`marker-gates-hint-never-repair`.** The `.ensure-applied` marker **gates the hint only** —
  it decides *whether to nudge*, and the nudge NEVER runs a sweep or repairs anything. The only
  thing that converges a store is `run_ensures` (init / `fsck --repair` / MCP boot). This keeps
  the write path cheap and side-effect-free.
- **Hot-path budget (≤1 read/process).** `pending = registry_ids() − applied_ids(tracker)` is
  computed once per process per store and cached as the pending id **set** (so the hint can
  name the pending units), so a converged store reads `.ensure-applied` **at most once** and
  does zero further reads on later writes — and never spawns a subprocess.
- **Rate-limited + suppressible.** When pending, it emits one WARNING naming the pending units
  and pointing at `rebar fsck --repair`, no more than once per `ensure.hint_interval_secs`
  (default 86400) via `.ensure-hinted`; `ensure.hint_enabled = false` silences it entirely.
  Env overrides auto-derive as `REBAR_ENSURE_HINT_INTERVAL_SECS` / `REBAR_ENSURE_HINT_ENABLED`
  (see [config.md](config.md) `[ensure]`).
- **Fail-silent + single choke point.** The hook is installed once, in
  `event_append.write_and_push`, through which `_seam.append_event` (comment/tag/edit/link/
  set_*/sign) and the composer create/edit/revert path funnel. It swallows **all** its own
  exceptions (incl. lazy-import) so a committed write never fails on it. The **accepted gap** —
  inline committers that do *not* funnel through `write_and_push` (`claim`/`transition` via
  `txn.*_core` + `push_after_commit`, `compact`, `delete`) — is intentionally **not** hooked; a
  rate-limited nudge is not a correctness mechanism, and those mutators still surface pending
  state on the next covered write.

## Accepted trade-offs

- **Long-lived MCP re-sweep.** The registry is swept at boot; a store that drifts *during* a
  long-lived server's life (e.g. a new unit shipped to a peer) is not auto-re-swept mid-life —
  converge it with `rebar fsck --repair` or a server restart. The write-path hint will surface
  the drift.
- **Post-rollback safety.** Rolling back to an older binary is safe: the git-ignored
  `.ensure-applied` / `.ensure-hinted` markers are simply ignored by old code, and the
  **committed** `.gitattributes` / `.gitignore` content stays on the tickets branch — harmless
  and idempotent (old code tree-checks before committing, so it neither re-commits nor errors
  on the extra content).
- **Rare double-hint** under parallel agents (two processes both past the interval) is accepted.

## A-tier ledger (future)

The convergent School-B registry above does **not** cover changes that are *irreversible* or
*unsafe to re-run* — those need an ordered, once-only ledger with a **committed store-format
version** that fails **closed** when a store is newer than the running binary (so an old binary
refuses rather than corrupts). That A-tier ledger is future work; the `gc-config` unit is the
canonical example of a change that fits School B today but would move under an A-tier version
gate if it ever became unsafe to re-run. (See the `doctor` idea `dabb`.)

## The committed store-compat record + fail-closed gate (story 21dd)

The A-tier ledger above is still future work, but its **committed store-format version that
fails closed** is now realized as a small, standalone record — `.store-compat.json` — on the
tickets branch:

```json
{"format_version": 1, "required_capabilities": []}
```

**How a v1.0 binary reads it (`rebar._store.compat`).** Before any *mutating or
externally-publishing* operation, `check_store_compat(tracker)` classifies the record into four
states:

1. **ABSENT** → implicit legacy (format version `0`), compatible, **passes through**. A store
   predating this feature is never blocked — this is what makes shipping the record purely
   additive and rollback-safe.
2. **PRESENT + compatible** → a known `format_version` and every `required_capabilities` entry in
   `KNOWN_CAPABILITIES` → pass.
3. **PRESENT + incompatible** → an unrecognized `format_version`, or a required capability this
   binary does not provide (the store was written by a *newer* rebar) → raise
   `StoreIncompatibleError` (fail **closed**).
4. **PRESENT + corrupt/unreadable** → JSON parse error, malformed shape, truncation, or a read
   error → raise `StoreIncompatibleError` naming the parse error + record path. A corrupt record
   is **never** silently treated as absent (that would let it bypass the gate).

**Where the gate fires.** The single chokepoint is inside `rebar._store.lock.acquire()` (after
tracker canonicalization, before the fcntl leg), so it covers `write_lock()` and every direct
`acquire()` caller — leaf writes (`_seam.append_event`), `txn`, `compact`, and the `fsck` repair
path. The two publishing paths that mutate the store **without** the write lock are gated
explicitly: `fsck_recover` (raw `git rebase/merge --continue`) and the reconciler's outbound
apply (`reconcile._apply_mutations`, guarded by `persist` so dry-run/cap-0 previews are exempt).

**Reads stay available.** The gate is on the *write* lock only, so `list`/`show`/`search` and
`fsck`'s read-only diagnostic keep working on an incompatible store. `fsck` surfaces the problem
as a structured top-level `compat_error` object (`{"kind", "detail"}`, where `kind` ∈
`unknown_format_version | unknown_capability | corrupt_record`) plus a stderr WARNING, **without**
changing its exit code — so an operator can still inspect a store the write gate refuses.

**The ensure unit stamps it.** The `store-compat` ensure unit (`init._store_compat_unit`) writes
the current-version record via `fsutil.atomic_write` and commits it to the tickets branch when the
committed blob is absent (tree-checked, so a converged store makes zero commits). Because an ABSENT
record passes the gate, the sweep's own `write_lock` acquisition on a fresh/legacy store is not
self-blocked. The record is a **committed** file — deliberately NOT in `.gitignore`.

**Fail-closed integrity in the sweep.** `run_ensures` wraps its body in a broad `except Exception`
that logs-and-returns (an ensure sweep must never abort its caller). `StoreIncompatibleError`
subclasses `Exception`, so it would be swallowed into a silent no-op — turning the gate into a
bypass at MCP boot / `fsck --repair`. `run_ensures` therefore **re-raises** it explicitly, ahead
of the broad handler, so the incompatible-store signal reaches the caller and fails closed.

**Rollback safety.** Rolling back to a pre-v1.0 binary is safe: an older binary does not read
`.store-compat.json` at all (it has no gate), and the committed record is inert to it. A v1.0
binary reading a *newer* store's record is exactly the fail-closed case above (refuse rather than
corrupt) — the intended one-way protection.

## Legacy signature-mirror retirement (352b)

The additive-attestations rollout (epic dark-acme-lumen) kept a legacy single-slot
`state['signature']` mirror alongside the authoritative kind-keyed `state['attestations']`
map, as the *expand* half of an expand/contract. Task **352b** ships the **contract** half:
new SNAPSHOTs stop carrying the mirror.

**What changed (introduced in 0.7.x, after 0.7.1).**

- **Consumers migrated (expand reinforcement).** Every in-tree reader of the legacy mirror now
  goes through `rebar.signing.most_recent_attestation(state)`, which returns the most-recent
  attestation of any kind from the `attestations` map (greatest `signed_at`, ties → last
  processed — exactly the old mirror's last-writer-wins), falling back to `state['signature']`
  only when the map is absent. Migrated: `signing._record_for_kind` (the `kind=None` /
  verify-latest path), `_engine_support/validate.py` (store-health signature check),
  `_commands/txn.py` (the `require_signature_for_close` gate).
- **Contract: new snapshots omit the mirror.** `compact.py` strips the legacy `signature`
  key from a SNAPSHOT's `compiled_state` (alongside the always-derived `updated_at`), so a
  freshly compacted or fsck-rebuilt ticket carries only `attestations`. Old snapshots that
  still hold only the mirror are upgraded on read by the existing fold-in
  (`_processors.py`), so migrated consumers always find a record.
- **Toggle removed (task 7ed9): never-emit is now hardcoded.** 352b shipped this behind a
  rollback lever (`compact.emit_legacy_signature_mirror`, default `false` = drop). Its
  default already meant "never persist the mirror", so its removal changes no runtime
  behavior — it only deletes the documented one-line config rollback. `compact.py` now
  unconditionally strips `signature` from every new snapshot, and the config key no longer
  exists (setting it warns + is ignored). **The in-memory re-derivation is untouched:** the
  reducer's `process_signature` still re-projects `state['signature']` from the attestations
  on every replay, so signature verification keeps working on a compacted, mirror-less ticket.

**The readiness gate (AC1) — why this is a one-way door.** A pre-attestations clone (code
older than the attestations release) reads `state['signature']` directly and has no
`attestations` map to fall back to. If such a clone reads a *new* (mirror-less) snapshot, its
verify / close gate sees no signature. Therefore the mirror must not be dropped until **every
clone and reconcile host is on ≥ the attestations release**. This deployment satisfies the
gate: the fleet auto-updates hourly to `origin/main`, so no pre-attestations binary remains
live. Confirm before shipping to any *other* environment (upgrade reconcile hosts first, as
`fsck` already warns for newer-than-binary event types).

**Rollback (task 7ed9 — the config lever is GONE).** 352b shipped this drop behind a
config rollback lever: `compact.emit_legacy_signature_mirror = true` (env
`REBAR_COMPACT_EMIT_LEGACY_SIGNATURE_MIRROR=true`) made the next compaction re-emit the
mirror in a ticket's snapshot, with no binary rollback. **That toggle has been removed.**
The key's default already meant "never persist the mirror", so its removal changes no
runtime behavior; it only retires the config escape hatch. Setting the key now warns and is
ignored. After this change, **new snapshots never persist the `signature` mirror and there is
no config flip that re-emits it** — the only way to recover the persisted mirror for a
mirror-less snapshot read by an old (pre-attestations) reader is a **code downgrade** to a
binary that still writes it. This is safe for this deployment precisely because the fleet
auto-updates hourly to `origin/main`, so no pre-attestations binary remains live to need the
mirror; the in-memory re-derivation (below) covers every migrated reader.

**A note on removing the read-side fold-in.** 352b intentionally **keeps** the
`_processors.py` old-snapshot fold-in. Removing it is a *further* contract step that is safe
only once no snapshot predating the `attestations` map remains in any store — a separate
readiness condition from the write-side drop. It is cheap and lossless to keep, so it stays.
