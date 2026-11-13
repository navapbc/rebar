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

### Fix-version (`releases`) sync, ADR 0124 — `test_cloud_fix_versions.py`

These run **real reconcile passes** (`python -m rebar.reconciler --mode bootstrap-strict`)
from a fresh, empty rebar store (`cloud_store` fixture), scoped with `--filter-local-ids` to
one throwaway issue each. Every pass fetches all of REB (about 2 minutes, measured
2026-11-12), so the 4 scenarios share 2 issues:

| Test | Scenarios | Assertion |
|---|---|---|
| `test_releases_round_trip_on_one_issue` | outbound add, outbound remove, inbound | each release added in rebar lands in `fixVersions` and earlier ones stay; the pass that adds the second and the pass that removes one each log `RECON: outbound_update ... releases=1` with no whole-field `fixVersions` write; a version set in Jira arrives in the ticket's `releases` |
| `test_missing_version_is_withheld_then_written` | missing version | a release naming a version REB lacks is not written and raises a `release-version-missing:<name>` BRIDGE_ALERT; once the version exists the next pass writes it and resolves the alert |

## Self-cleaning contract (why it is safe against the shared REB project)

- Every issue is created with a **unique** `rebar-id:<local_id>` label and immediately
  stamped with the run-scoped `REBAR_PROBE_RUN_LABEL` (`rebar-id:cloudprobe-<run_id>` in CI).
- The owning test **deletes its issue by key in a `finally`** — the primary teardown.
- `conftest.py`'s autouse label sweep and the workflow's always-run `acli … delete --yes`
  step are crash backstops keyed on the SAME run label, so a crash between create and delete
  never leaks.
- **Fix versions** get run-scoped names, `<run prefix>-<label>-<hex>`, where the run prefix is
  the run label without `rebar-id:` (`cloudprobe-<run_id>` in CI). The owning test deletes
  each version in `finally`; the autouse backstop and the workflow step also delete every REB
  version whose name starts with the run prefix.
- A fix-version test's issue is created by its binding pass, which labels it only
  `rebar-id:<local_id>`; the run label is added straight after. Its summary carries the run
  prefix, so the backstops also delete REB issues whose summary contains it, and `finally`
  deletes by the `rebar-id` label if the binding was never recorded.
- The coordinator tests hand the coordinator **exactly one plan**, with no enumeration. The
  fix-version tests run full passes instead, and are bounded to one issue by
  `--filter-local-ids`, which dispatches mutations only for the selected ticket and its
  bound key.

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
this suite, and on failure/cancel sweeps issues carrying `REBAR_PROBE_RUN_LABEL` or the run
prefix in their summary, and REB fix versions whose name starts with the run prefix. The
cleanup can be confirmed afterwards with `GET /rest/api/3/project/REB/version?query=<run prefix>`,
which should return no versions.
