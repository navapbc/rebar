# ADR 0037 — Reconciler live-validation harness (guaranteed bilateral teardown + deterministic matrix)

**Status:** Accepted (epic adept-hedge-stain / d01e)
**Date:** 2026-07-09

## Context

The Jira reconciler is a bidirectional, level-triggered controller with many correctness
invariants that only fully manifest against a *live* Jira tenant: both-sides conflict
recording (C1), allowlist-drop alerting (C3), 429 backoff (C4), echo suppression, lossy
status round-trips, the absence lifecycle (probe → confirmed-404 → grace → soft-retire),
hard-delete → re-create with a re-stamped identity, and the blast-radius breaker. We need a
comprehensive validation matrix that (a) is trustworthy, (b) never strands synthetic
artifacts in the shared REB Jira project or the local tickets store, and (c) can run in CI
without a live tenant.

Two forces are in tension:

1. **Live fidelity.** Some behaviors (delete-permission scope, eventual-consistency timing)
   are only real against Jira. A live run mutates a *shared* project, so a leaked test issue
   is real pollution, and a create+immediate-mutate races Jira's index.
2. **Determinism / CI.** The bulk of each invariant is implemented in *pure* reconciler seams
   (`classify.census`, `outbound_fields._diff_fields` with its `conflict_sink`/
   `dropped_field_sink`, `acli_subprocess._rate_limit_backoff`, `binding_store.note_absent`,
   the status-annotation-label helpers, the inbound echo/marker filter). Those can — and
   should — be asserted deterministically, with no network.

## Decision

Split the matrix into a **deterministic core** driven against the real reconciler seams,
plus a small number of **`@_requires_live`** probes that actually mutate REB, all under one
**guaranteed bilateral teardown** harness. The file is `tests/integration/test_reconcile_live_e2e.py`,
marked `pytestmark = [integration, live]` (excluded from `make test`'s
`-m "not integration and not external"`; run explicitly with `-m live`).

### 1. `ArtifactTracker` + `_bilateral_teardown` (partial-setup aware, leak-loud)

- `ArtifactTracker` records exactly which synthetic Jira keys / local ids were *actually*
  created, so a create that fails halfway leaves only what landed — teardown removes precisely
  those, never a hard-coded list.
- `_bilateral_teardown` deletes every tracked artifact from **both** systems, retrying each
  with bounded exponential backoff (`_retry`). It runs to completion even if one delete throws
  (a mid-teardown error never strands the rest). On exhaustion the id is appended to
  `leaked-artifacts.log` (a CI artifact) and returned, so the run can exit non-zero — **a leak
  is loud, never silent.** Invoked from a `finally`, so an assertion failure or exception still
  cleans up.
- The live probe routes its `leaked_log` to `tmp_path` so a leak never writes into `REPO_ROOT`
  (the repo-root-leak guard fixture).

### 2. One observable pass criterion per scenario, against the REAL seam

Each matrix scenario asserts a single observable criterion against the exact code that
implements it (see the module docstring for the seam per scenario). The deterministic
scenarios need no network; they run green offline and are the durable, re-runnable proof.

### 3. Eventual-consistency discipline for live probes

Jira's search index lags both a create and a delete by an **unbounded** interval. Two rules
fall out and are encoded in the harness:

- **Before mutating a just-created issue, poll until it is index-visible** (`_poll_until_visible`,
  key search then summary fallback) — a create+immediate-delete otherwise races the index and
  acli emits a *confusing* `AcliMutationError("Issue does not exist or you do not have
  permission to see it")` (NOT a 403), which reads as a false permission failure.
- **Never assert on post-mutation index state.** The authoritative signal for the
  delete-permission probe is the *delete's own return value* (`{"status": "deleted"}`; acli
  raises a loud `PermissionError` on a real 403). A follow-up search-is-empty (or a re-delete)
  assertion is flaky because the index-convergence delay is unbounded and a re-delete of an
  already-gone key re-raises the same eventual-consistency false-failure.

### 4. Report

`scripts/run_live_matrix.py` is the one-command entry point: it runs
`pytest -m live --junitxml=reports/d01e-live-matrix.junit.xml` (the JUnit XML, a CI artifact
path) and derives the JSON summary at `reports/d01e-live-matrix.report.json` (totals +
per-test outcome/duration). Both are gitignored run artifacts; the
durable evidence (pass/fail table + live run ids) is recorded on ticket d01e.

## Consequences

- The matrix runs **green offline** (deterministic core) AND validates the genuinely-live edge
  (delete-permission) against REB with guaranteed cleanup — best of both.
- Zero shared-project pollution: partial-setup-aware tracking + finally-teardown + a loud leak
  log. Every scenario is followed by a `summary ~ "DELETE-ME"` sweep.
- The seams are loaded via `sys.path.insert(0, .../rebar_reconciler/..)` + package import (the
  reconciler is not installed top-level). Story **eca4** replaces that path shim with a proper
  package import; this harness is one of its importers and moves with that sweep.
- Follow-up: the `binding-retired` alert record omits `timestamp_ns`, so `alert_store.is_deduped`
  cannot see it (its dedup is effectively a no-op); the tombstone test asserts the alert *record*
  directly instead. Tracked as a reconciler observability nit, out of scope for d01e.
