# Read-only Jira Cloud + S3 multi-project rehearsal (story `lacelike-corked-tattler`)

A LIVE, opt-in acceptance rehearsal for rebar's many-to-many Jira bridge over the **S3
store backend** and **real Jira Cloud volume/diversity**. It complements the Jira Data
Center harness in `../live_jira_dc` (sibling story 368f): DC is git/file-backed and uses
low-volume throwaway projects, so it can exercise neither the S3 backend nor real Cloud
volume. This suite drives the reconciler against the two REAL Cloud projects **REB** and
**DIG** mapped into ONE isolated, S3-backed store.

## Cardinal rule — READ-ONLY on Jira Cloud

This suite must NEVER write to Jira Cloud. That invariant is enforced three ways:

1. **Structurally** — `conftest.py::readonly_jira_guard` (autouse) monkeypatches every
   mutating method on the Cloud transport class (`create_issue`, `update_issue`,
   `add_label`, `set_entity_property`, `transition_issue_by_name`, `delete_issue`, …) to
   raise `JiraWriteForbidden`. A scenario is literally unable to invoke an outbound Jira
   mutation. `test_read_only_guard_is_real` proves the guard is not vacuous.
2. **By construction** — the only Jira-touching code paths used are read-only:
   - `rebar_reconciler.fetcher.compute_snapshot` — the inbound fetch that WRITES NOTHING
     (it only issues JQL searches); its per-project JQL fan-out is driven by the store's
     `projects.json` mapping.
   - `rebar.bridge_preview` — a dry run (`Mode.DRY_RUN` → `MODE_CAPS = 0` → `persist =
     False`), so no leaf applier runs and nothing is written to Jira or the store.
   The suite never calls `bridge_sync` or `reconcile` in a persisting mode (any persisting
   mode writes back to Jira on inbound-create).
3. **By measurement** — the Jira-touching scenarios assert per-project issue counts are
   identical before and after (a fresh-client read-only oracle).

## Isolation

Each test builds a FRESH, minimal store whose ONLY git remote is a throwaway
`s3://<bucket>/<unique-prefix>`, pinned as `sync.remote` with `REBAR_SYNC_PUSH=off`. Store
CONTENT is irrelevant to a read-only fetch (the volume comes from live Jira, not the local
store), so a minimal store is the correct, lighter isolation boundary — it cannot reach the
production tickets remote. The S3 prefix is deleted on teardown.

## The five validations (→ scenarios)

| # | Validation | Scenario |
|---|---|---|
| 1 | one S3 store maps BOTH projects | `test_one_store_maps_both_projects` |
| 2 | inbound fetch pulls BOTH at real volume, per-project attribution | `test_inbound_fetch_pulls_both_projects` |
| 3 | read-only + scoped per project (no cross-project contamination) | `test_fetch_is_scoped_per_project`, `test_bridge_preview_read_only_and_scoped` |
| 4 | the S3 backend round-trips with the mapping intact | `test_s3_store_roundtrips_with_mapping_intact` |
| 5 | ZERO Jira mutations (structural + before/after counts) | every test; proven non-vacuous by `test_read_only_guard_is_real` |

Plus a negative control from the prior-failure lesson (the deleted harness at git
`ab9c452dd3` seeded non-existent project keys and expected an inbound fetch to succeed):
an UNKNOWN mapped project (among several) is SKIPPED and the pass continues over the
others — `test_unknown_project_skips_and_continues`.

## Running it

Prerequisites:

- Live Jira Cloud creds in the environment: `JIRA_URL`, `JIRA_USER`, `JIRA_API_TOKEN`, and
  the `acli` binary on PATH, authenticated for read-only queries against REB + DIG.
- The `git-remote-s3` helper on PATH (`uv pip install git-remote-s3==0.3.2`) and an AWS
  identity that can read/write the throwaway rehearsal bucket.

```sh
export REBAR_RUN_EXTERNAL=1                 # the external tier is inert without this
export AWS_PROFILE=frontier AWS_REGION=us-east-1
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.bareRepository GIT_CONFIG_VALUE_0=all
pytest -m external tests/external/live_jira_cloud_s3 -q -rA
```

The whole suite runs in ~16 minutes (dominated by the full-project Cloud fetches over
`acli`).

### Environment / configuration knobs

| Var | Purpose | Default |
|---|---|---|
| `REBAR_RUN_EXTERNAL` | opt-in gate for the whole external tier | (unset → skip) |
| `JIRA_URL` / `JIRA_USER` / `JIRA_API_TOKEN` | live Cloud creds (read-only) | — |
| `REBAR_REHEARSAL_S3_BUCKET` | bucket the throwaway per-run prefix is created under | `rebar-rehearsal-368f-896586841071` |
| `REBAR_REHEARSAL_S3_REMOTE` | an explicit `s3://…` prefix (overrides the generated one) | (generated) |
| `AWS_REGION` / `AWS_PROFILE` | AWS identity + region for the S3 store backend | — |

## CI

The `jira-cloud-s3-rehearsal` job in `.github/workflows/external-integration.yml`
(`workflow_dispatch` + weekly `schedule`) runs this suite on a native amd64 runner. It is
**gated on `vars.AWS_S3_REHEARSAL_ROLE_ARN`** — an S3-capable OIDC role that must be
provisioned separately, because the Bedrock CI role (`vars.AWS_BEDROCK_CI_ROLE_ARN`) has NO
S3 permissions. Until that role variable is set the job does not run, and the authoritative
acceptance evidence is a clean LOCAL live run instead. The general `external` job
`--ignore`s this directory so its `jira_live` all-skip cannot be masked by the Cloud tests
executing there.
