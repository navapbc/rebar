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

The five built-in units: `env-id`, `gc-config`, `merge-ours`, `gitattributes`, `gitignore`.

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
