# Bounded, self-cleaning live Jira Cloud coordinator MUTATION probe

The mutating counterpart to the read-only `../live_jira_cloud_s3` rehearsal. It proves the
RP-03 create-coordinator's outbound **write** paths against **real Jira Cloud** — the
evidence the RP-03 epic's Live-External Integration AC requires and that a read-only suite
structurally cannot provide.

## What it exercises (through the shipped facades, not test doubles)

| Path | Facade driven | Assertion |
|---|---|---|
| create + binding lifecycle + commit-unknown | `create_route.run_coordinated_outbound_create` | a key lands, the `rebar-id:<local_id>` label + `local_id` property attach, containment confirms, dependents release (AC5), and the binding is searchable under the index-lag backoff |
| non-create fuse pass | `batch_dispatch.coordinate_and_fuse` | a single-plan live update tallies one `applied`, raises no fuse decision, and is non-degraded |

## Self-cleaning contract (why it is safe against the shared REB project)

- Every issue is created with a **unique** `rebar-id:<local_id>` label and immediately
  stamped with the run-scoped `REBAR_PROBE_RUN_LABEL` (`rebar-id:cloudprobe-<run_id>` in CI).
- The owning test **deletes its issue by key in a `finally`** — the primary teardown.
- `conftest.py`'s autouse label sweep and the workflow's always-run `acli … delete --yes`
  step are crash backstops keyed on the SAME run label, so a crash between create and delete
  never leaks.
- The coordinator is handed **exactly one plan** — no enumeration, no `--filter-local-ids`
  legacy path — so the blast radius is structurally one issue.

## Gating (all off the default lane)

1. `tests/external/conftest.py` skips the whole external tier unless `REBAR_RUN_EXTERNAL=1`;
2. `live_jira_ready()` skips when Jira creds or the `acli` binary are absent;
3. the module-level `_live_jira_ready` sentinel enrols the suite in the **all-skip canary**,
   so a collected-but-fully-skipped run FAILS rather than reporting a hollow green.

## Running it locally

```bash
export JIRA_URL=... JIRA_USER=... JIRA_API_TOKEN=...   # live Cloud creds
export JIRA_PROJECT=REB                                # the throwaway project
export REBAR_RUN_EXTERNAL=1                            # the tier is inert without this
# optional: pin the run label so leftovers are sweepable by a known label
export REBAR_PROBE_RUN_LABEL="rebar-id:cloudprobe-local-$(date +%s)"
pytest -m external tests/external/live_jira_cloud_mutation -q -rA
```

`acli` must be on PATH and authenticated. The JQL visibility backoff is tunable via
`JIRA_PROBE_JQL_RETRIES` / `JIRA_PROBE_JQL_SLEEP` / `JIRA_PROBE_JQL_SLEEP_MAX`.

## CI

The `jira-cloud-mutation-probe` job in `.github/workflows/external-integration.yml` runs
this suite on `workflow_dispatch` and the weekly schedule whenever `vars.JIRA_URL` is set:
it installs + authenticates `acli`, runs `rebar bridge check-access` as a preflight, then
this suite, and always sweeps `REBAR_PROBE_RUN_LABEL` on failure/cancel.
