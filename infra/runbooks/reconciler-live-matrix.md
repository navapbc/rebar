# Runbook — reconciler live-validation matrix (run · interpret · clean up)

Scope: operating `tests/integration/test_reconcile_live_e2e.py`, the reconciler's
live-validation matrix. The architecture and the reasons behind these rules are
[ADR 0085](../../docs/adr/0085-reconciler-live-validation-harness.md) (archived into
[ADR 0121](../../docs/adr/0121-reconciler-architecture-consolidated-record.md) §20); this runbook is the operator
procedure.

The matrix is a **deterministic core** that needs no network, plus a small number of
`@_requires_live` probes that actually create and delete issues in the REB project. Both
run under one guaranteed bilateral teardown harness.

## Run it

One command produces both report artifacts:

```sh
python scripts/run_live_matrix.py
```

It runs `tests/integration/test_reconcile_live_e2e.py -m live` with `--junitxml` pointed at
`reports/d01e-live-matrix.junit.xml`, then derives the JSON summary at
`reports/d01e-live-matrix.report.json` (totals plus one entry per test with outcome and
duration). Both are gitignored run artifacts. It exits with pytest's return code, or **2**
if the JUnit XML was never produced.

Extra pytest arguments pass through after `--`:

```sh
python scripts/run_live_matrix.py -- -k c4 -q
```

The module is marked `pytestmark = [integration, live]`, so `make test` excludes it
(`-m "not integration and not external"`). To invoke pytest directly, select it explicitly:

```sh
pytest tests/integration/test_reconcile_live_e2e.py -m live
```

### Environment

| Variable | Effect |
| --- | --- |
| `JIRA_URL`, `JIRA_USER`, `JIRA_API_TOKEN` | All three, plus `acli` on `PATH`, are required by the `@_requires_live` probes. With any missing, those probes **self-skip** and the deterministic core still runs green. |
| `RECONCILER_E2E_INDEX_VISIBILITY_TIMEOUT_S` | Overrides the index-visibility poll budget. A malformed or non-positive value falls back to the built-in default rather than degrading the poll to a zero budget. |

Running with no live environment at all is a supported mode, not a degraded one: the
deterministic scenarios are the durable, re-runnable proof.

## Eventual-consistency discipline

Jira's search index lags both a create and a delete by an **unbounded** interval. Two rules
are encoded in the harness, and any new scenario must follow them:

- **Before mutating a just-created issue, poll until it is index-visible**
  (`_poll_until_visible`, key search then summary fallback). A create followed by an
  immediate delete otherwise races the index, and `acli` emits a *confusing*
  `AcliMutationError("Issue does not exist or you do not have permission to see it")` —
  **not** a 403, though it reads as a false permission failure.
- **Never assert on post-mutation index state.** The authoritative signal for the
  delete-permission probe is the *delete's own return value* (`{"status": "deleted"}`;
  `acli` raises a loud `PermissionError` on a real 403). A follow-up search-is-empty
  assertion, or a re-delete, is flaky: the convergence delay is unbounded, and re-deleting
  an already-gone key re-raises the same eventual-consistency false failure.

If a probe fails with that `AcliMutationError` text, treat it as an index-lag symptom
first — raise `RECONCILER_E2E_INDEX_VISIBILITY_TIMEOUT_S` and re-run — before treating it
as a permissions problem.

## Cleanup and the leak log

`ArtifactTracker` records exactly which synthetic Jira keys and local ids were *actually*
created, so a create that fails halfway leaves only what landed. `_bilateral_teardown`
deletes every tracked artifact from **both** systems, retrying each with bounded
exponential backoff, and runs to completion even if one delete throws. It is invoked from
a `finally`, so an assertion failure or an exception still cleans up.

On retry exhaustion the id is appended to `leaked-artifacts.log` and returned, so the run
can exit non-zero — **a leak is loud, never silent.** The live probe routes its
`leaked_log` to `tmp_path` so a leak never writes into the repo root.

**If `leaked-artifacts.log` is non-empty after a run**, delete the named keys by hand
before the next run, then truncate the file. Every scenario is also followed by a
`summary ~ "DELETE-ME"` sweep, so a stray artifact from an interrupted run is picked up by
the next full pass.
