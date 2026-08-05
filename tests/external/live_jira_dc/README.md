# Jira Data Center verification harness (story J5, epic e369)

A real Jira 8.17.1 Data Center instance you can stand up locally or in CI, used as the
verification oracle for rebar's DC reconciler adapter. There is no way to buy a Jira DC
license to test against — Atlassian ended new DC purchases and no longer issues DC trial
licenses — and there is no official OpenAPI/Swagger spec for Jira DC either, so this harness
exists to give the DC adapter something real to be verified against instead of only a fake
that encodes our own assumptions.

## What this is

- Base image: `addono/jira-software-standalone`, pinned **by digest**:
  `sha256:04326628dc4ac36b2bfc1d0f2ebe5ba3807c1ec9cf9b18307d3c2ad7222537e9`.
- Jira version: **8.17.1** (`>= 8.14`, the release that introduced Personal Access Tokens, so
  the PAT bearer-auth path the DC transport uses is exercised for real).
- Default credentials: `admin` / `admin`.
- Served at `http://localhost:2990/jira` (loopback-only — see "Security" below).
- The `./Dockerfile` here is a **vendored** reproduction (attribution: pycontribs/jira,
  `docker/jira-test-image/Dockerfile`) of the wrapper the pycontribs/jira project uses for
  exactly this problem. It is vendored rather than built from a clone of that repo, or pulled
  as `pycontribs/jira-test-image:8.17.1`, because that tag **is not published to any
  registry** — it is upstream's own local build name (their README's own instruction is
  `docker build -t pycontribs/jira-test-image:8.17.1 docker/jira-test-image`). Only the
  `addono/jira-software-standalone` base is actually pullable, and that is what is pinned by
  digest above.

## Important: pinning the image digest does NOT pin Jira

**The image does not contain Jira.** `atlas-run` downloads Jira 8.17.1 from
`maven.atlassian.com` **at container-start time** — verified by watching the container log
show `Configured Artifact: com.atlassian.jira.plugins:jira-plugin-test-resources:8.17.1:zip`
followed by roughly 917 `Downloading from atlassian-public: https://maven.atlassian.com/...`
lines before Tomcat and H2 come up. Consequences:

- **Egress to `maven.atlassian.com` is a hard runtime dependency**, not a one-time image pull.
  An egress-restricted or air-gapped runner cannot start this harness at all.
- If Atlassian ever withdraws the 8.17.1 artifacts from their public Maven repo, the harness
  stops working **even though the base image digest above is still pinned**. That is the real
  fragility here, not the image — and it is the reason real-instance validation is tracked
  separately as ticket `9895-ac7d-4276-4f08` (this README records the exact image/version so
  that future comparison has a baseline).
- First start is dominated by that download, not JVM boot, so the readiness budget below is
  generous by default.
- A persistent Maven cache volume (`docker-compose.yml`'s `jira-dc-m2-cache`, mounted at
  `/root/.m2`) makes repeat starts much faster; without it every fresh container re-downloads
  the full ~900-artifact set.

## Version and architecture constraints

- **Only Jira 8.x works on this base.** `JIRA_VERSION` is overridable across 8.x patch
  versions, but 9+/11+ fail Maven resolution — the upstream pom `atlas-run` runs is anchored
  to the 8.x dependency graph. Do not bump `JIRA_VERSION` past 8.x expecting it to track a
  client's newer Jira.
- **The base image is `linux/amd64` only.** `docker-compose.yml` sets `platform: linux/amd64`
  explicitly so behavior is deterministic rather than dependent on the host's default platform
  resolution. **A native amd64 runner is a hard requirement for a run that completes**, not a
  preference: on an emulated arm64 host (Apple Silicon, some ARM CI runners), Jira does not
  become usable within an hour, even with the Cargo start-timeout override in place — measured
  directly, three separate attempts, on this project's own arm64 development workstation:
  1. Plain upstream entrypoint: container killed at ~25 minutes by Cargo's own default 600000 ms
     deploy watchdog ceiling (`DeployerWatchdog: ... failed to finish deploying within the
     timeout period [600000]`), even though Jira had genuinely booted (Tomcat, H2, and the
     plugin system were starting).
  2. `-DstartupTimeout` / `-Dcargo.timeout`: died identically at the same `[600000]` line —
     these property names have **no effect** on the AMPS/`jira-maven-plugin` run goal.
  3. `-Dproduct.start.timeout=2700000` (the correct property, per Atlassian's own docs): the
     watchdog no longer killed the container, and it ran 55 minutes — but never reached
     "Starting the JIRA Plugin System" in that time. Emulation, not configuration, was the wall.

  GitHub's `ubuntu-latest` runners are native x86_64, so the `.github/workflows/
  external-integration.yml` `jira-dc-harness` job runs this harness WITHOUT emulation — that is
  the primary intended home for this harness's acceptance evidence, not a local run on
  Apple Silicon.

## Licensing

**No licensing claim is made here.** No license key is supplied to build or start this
instance; the Atlassian SDK's `atlas-run` provisions a local developer instance on its own.
Whether that constitutes a bundled license is a legal characterization this story has not
verified, so this README describes only the observed behavior above.

## Security

The compose file publishes the instance on **`127.0.0.1:2990:2990`** — loopback-scoped, not
the bare `2990:2990` Docker binds to `0.0.0.0` by default. This instance ships well-known
`admin`/`admin` credentials, so it must never be reachable from the wider network.

## Running it

Locally (native amd64 strongly preferred; see the arm64 findings above):

```sh
make jira-dc-up
REBAR_RUN_EXTERNAL=1 pytest tests/external/live_jira_dc/ -q
make jira-dc-down
```

`make jira-dc-up` always brings up a **fresh** instance (`--force-recreate`), so an
interrupted previous teardown cannot leak stale Jira state into the next run.

Without `REBAR_RUN_EXTERNAL=1`, everything under `tests/external/` — including this
directory — is inert by design (see `tests/external/conftest.py`'s `_require_external_opt_in`
guard): the suite reports `skipped`, never a hard failure or a raw connection traceback.

### Readiness

**Session readiness asks exactly one question: does Jira answer REST at all?**
`conftest.py::wait_for_jira_dc_ready` polls `/rest/api/2/serverInfo` with a **default
20-minute budget** and a ~5 second poll cadence, overridable via `JIRA_DC_READY_TIMEOUT`
(seconds). The generous default is deliberate: an emulated amd64-under-arm64 base plus a
~900-artifact Maven download makes upstream's quoted "3-5 minutes" unattainable on an arm64
runner. On expiry it fails with an explicit "harness not running / not ready" message naming
`make jira-dc-up` — never a raw connection traceback.

**It does not — and must not — wait for the Epic custom fields.** GreenHopper provisions
`Epic Link` and `Epic Name` **when the first Jira Software project is created**, not when the
plugin starts. Measured on GitHub Actions experiment run `30981084637` against this image:

```
[before]            HTTP 200 total=27 customfield_count=0   EpicLink=False EpicName=False
[after-180s-quiet]  HTTP 200 total=27 customfield_count=0   EpicLink=False EpicName=False
create project -> HTTP 201 {'id': 10000, 'key': 'RBJEXPT'}
[after-create+0s]   HTTP 200 total=55 customfield_count=13  EpicLink=True  EpicName=True
VERDICT: elapsed_after_create=0.0512
```

So a 27-field, zero-`customfield_*` inventory is the **normal, healthy** state of a fresh
instance with no project — and a session-start wait for those fields is a deadlock, not a slow
path: it waits for something only the action it blocks can produce. Bug
`9790-cafa-dffa-462e` added exactly such a gate and it errored **all 62 cells at fixture
setup** (run `30975323866`); tripling its allowance to 1800s changed nothing (run
`30978613228` expired after 181 polls on a byte-identical inventory), while the run
immediately before it landed passed **62/62** (run `30964805133`). Corrected by bug
`941b-f049-5f29-4410`.

**Where the Epic capability IS checked:** `conftest.py::_assert_project_capabilities`, which
runs immediately after `_create_scratch_project` — the first moment the fields can exist. It
is a bounded **wait**, not a single read (0.0512s is fast but not atomic with the create's
`201`, so one read can lose the race), it raises `AssertionError` if the fields never appear,
and on success it prints the elapsed time, poll count and resolved field ids to the job log.
Its allowance is **`JIRA_DC_FIELD_READY_TIMEOUT` (seconds), default 120** — ~2400x the
measured 0.0512s, so it has enormous headroom while still failing a genuinely broken image in
two minutes. Override it per-invocation:

```bash
REBAR_RUN_EXTERNAL=1 JIRA_DC_FIELD_READY_TIMEOUT=300 \
  pytest -m external tests/external/live_jira_dc -q -rA
```

The definition is shared with the deterministic probe
(`scripts/jira_dc_epic_link_clear_probe.py`) via `scripts/jira_dc_field_readiness.py`, so
there is exactly one answer to "are the Epic fields registered?" — and the probe creates its
project before asking, for the same reason.

**Reading a failure.** The message dumps the field inventory it observed; that dump is the
decision procedure, and waiting longer is never the answer:

| What the inventory shows | What it means | What to do |
| --- | --- | --- |
| **No `customfield_*` entries at all** | The check ran before any Jira Software project existed on the instance | A code fault, not an environment one — the call site is in the wrong place (this is bug `941b-f049-5f29-4410`) |
| **Other `customfield_*` entries present, but not `Epic Link`/`Epic Name`** | Provisioning happened; this image genuinely no longer ships the Epic fields | A real degrade — re-run the capability map against the current image and update `_REQUIRED_FIELDS` |
| **A non-200 status, or a body that is not a list** | The inventory could not be read at all | Treated as not-ready (never a vacuous pass); the message records the HTTP status actually received |

## Feasibility evidence (recorded reproducibly)

- Base digest: `sha256:04326628dc4ac36b2bfc1d0f2ebe5ba3807c1ec9cf9b18307d3c2ad7222537e9`
  (`addono/jira-software-standalone`).
- Resolved image size when first built: ~879 MB.
- Host architecture the vendored Dockerfile was proven to BUILD and START-CONTAINER on:
  arm64 (Apple Silicon, under emulation). The container starts and stays up; it does not
  reach a fully answering REST endpoint within an hour on that host (see the three measured
  runs above) — this harness's live-answering acceptance evidence is produced on a native
  amd64 runner (`ubuntu-latest` in `.github/workflows/external-integration.yml`), not on this
  arm64 workstation.
- Observed startup phases (from container logs, arm64 host): image pull/build → `atlas-run`
  starts → `Configured Artifact: com.atlassian.jira.plugins:jira-plugin-test-resources:
  8.17.1:zip` → ~917 `Downloading from atlassian-public: https://maven.atlassian.com/...`
  lines → `JIRA Build : 8.17.1#817001-sha1:36e93c711dfb...` → Tomcat 8x starting → H2
  configured → "Starting the JIRA Plugin System" (reached only with `-Dproduct.start.
  timeout` raised, and only observed to be reached on a native-amd64 run).

## Known limitation: version drift from a real DC tenant

8.17.1 is older than a client is likely running in production, so this harness proves REST v2
protocol semantics (auth mode, field shapes, wiki-markup-not-ADF descriptions), not
deployment-specific behavior (workflow names, custom fields, permission schemes, user
directories). Closing that gap requires access to a real instance and is tracked separately as
ticket `9895-ac7d-4276-4f08`; this README's recorded image tag and digest give that
comparison a baseline.

## CI

`.github/workflows/external-integration.yml` runs this harness in its own `jira-dc-harness`
job (`runs-on: ubuntu-latest`, native amd64 — no emulation). It needs **no LLM API key** (the
pre-existing `external` job's LLM-key requirement does not apply here). The job builds the
vendored Dockerfile, waits on the readiness probe (up to ~20 minutes), runs only
`tests/external/live_jira_dc/`, dumps container logs on failure, and tears the stack down
under `if: always()`. It triggers the same way the rest of this workflow does:
`workflow_dispatch` plus the weekly cron — never on a per-commit push.
