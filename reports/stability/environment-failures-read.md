# Environment failures — 2026-09-01 (mechanism read)

Ticket `floaty-imperfect-pomeranian` (`0880-0afb-7fe3-48c9`), read (c) of three, under epic
`wide-wimpy-insect` (Track I — reduce defect introduction).

R2S3 found environment friction roughly doubling in its sample (4 → 9 of 13/60 session logs) and
left the cause unexplained. This is the mechanism read, with the classes grouped by whether a
local pre-flight check could actually catch them.

## Headline

- **One pre-flight check would cover roughly half of it.** Classes C2, C3 and C4 — missing or
  wrong local install, credential/entitlement failures, and ambient environment poisoning —
  account for **42 of ~90** identified incidents, and all three are deterministic and detectable
  on the developer's own box before a billable gate starts.
- **The single largest mechanism is the venv**: 30 logs record an ambient `PATH` without
  `mypy`/`ruff`/`pytest`/`timeout`, or a stale global `rebar` shadowing the checkout's build.
- **A meaningful fraction is not environmental at all.** Class C9 is agent-loop and harness
  defects that get *filed* as environment problems — tool max-retries-of-1, a search tool
  returning "No results found" mid-turn, and checks that can never fire. Separating them matters
  precisely because the label hides them.

## Method

Two sources, neither keyword-guessed.

1. **Automated `llm-degrade NEEDS_INVESTIGATION` session logs** carry a structured JSON body
   (`exception_type`, `message`, `model`), so they are countable without heuristics. 46 logs,
   parsed to 53 records, 2026-07-11 .. 2026-09-01.
2. **The ops error-sweep ledger** (`tomophobic-stilllife-mayfly`, four sweeps 2026-08-29 ..
   09-01), read per case.

Plus a scan of all 400 `session_log` tickets for operator-environment traps, counting each log
once. Command: `rebar session-logs --limit 400 -o json`. The JSON is extremely verbose — each
ticket carries a full authorship ledger with base64 DSSE envelopes — so fields were projected
before analysis rather than dumped.

## Source 1 — structured degrade records (n=53)

| exception | status | model | n | mechanism |
|---|---|---|---|---|
| `overload` | 529 | (unset) | 18 | provider capacity shed |
| `DDGSException` | – | bedrock opus-4-8 | 7 | web-search tool returned "No results found." inside an agent turn |
| `LLMConfigError` | – | mixed | 8 | missing `[agents]` extra (`pydantic-ai-slim`) or unresolvable auth method |
| `ModelAPIError` | – | anthropic sonnet-4-6 | 4 | generic "Connection error." |
| `ModelHTTPError` | 400 | anthropic sonnet-4-6 | 4 | **credit balance too low** |
| `UnexpectedModelBehavior` | – | opus-4-8 | 4 | tool (`read_file`, `list_directory`) exceeded max retries of 1 |
| `EndpointConnectionError` | – | bedrock sonnet-4-6 | 2 | could not reach `bedrock-runtime.us-east-1.amazonaws.com` |
| `ModelHTTPError` | 520 | anthropic sonnet-4-6 | 2 | Cloudflare 5xx in front of the provider |
| `ModelHTTPError` | 401 | anthropic opus-4-8 | 1 | "API key is invalid." |
| `ModelHTTPError` | 403 | bedrock opus-4-8 | 1 | AWS `AccessDenied` for `rebar-gerrit-instance-role` |
| `TypeError` | – | opus-4-8 | 2 | client-side type error in the runner |

## Source 2 — ops sweep, per case

- **`/var/gerrit` data disk at 94%**, alarm unrecovered since 2026-08-26 17:55; root disk flapped
  ~18× peaking 98%. Common cause: repo/pack growth from ~08-26 15:00. (P1 `5a05`.)
- **Verify Authorship Identity red**: checkout pack 481 → 489.85 MiB against a 100 MiB limit at
  `verify-identity.yml:47`; clean green→red boundary 2026-08-23 01:20. A raise to 550 MiB merged
  but **was not reflected in the CI environment**. (`a453`.)
- **Test Suite mirror red on macOS py3.13 and lower-bound py3.11**: `test_release_guards.py`
  assumes an **editable** install; the non-editable sweep resolves the `scripts/` path into
  `.venv` and 404s. Plus a lower-bound hatchling floor. Chronic across 12+ runs. (`e3e0`.)
- **Windows leg**: `ModuleNotFoundError: No module named 'fcntl'` at collection. **py3.14 leg**:
  `test_doctor_locks` reaches the network unmocked under a network-forbidden policy. (`0b31`.)
- **Terraform Drift red**: runner TF 1.10.5 < `required_version ">= 1.11"`, so `init` fails before
  `plan`. (`d227`.) Separately, 3 `mcp-client-pat` SSM params absent from state with no import
  block (`9350`).
- **Bridge run failures**: ACLI Jira search nonzero / 120 s timeout (`97e2`).
- **Autodeploy unhealthy**: `deploy_errors` ×11, `mcp-retire-cap` ×11, review-interrupts
  signal-unavailable ×2 (`2f46`).
- **Review-bot zombie** (`9d40`): compose recreate SIGTERMs the bot; uvicorn closes its LISTENING
  socket at the start of graceful shutdown, but lifespan shutdown never returns because in-flight
  reviews keep running. The container reports `Up`, the worker keeps voting for ~18 minutes, and
  nothing is bound to port 8000. Dispatch was dead 22 minutes, and the Gerrit webhooks plugin is
  not at-least-once (ADR 0009), so events in that window were **dropped silently**. The shipped
  MCP server is *not* vulnerable — its `drain_then_exit` is bounded by `grace_seconds`.
- **`setup-uv` manifest** (`5caa`): pinning uv removed only one of two manifest consumers;
  `setup-uv` still calls `getArtifact(...manifestUrl)` unconditionally, so the fetch-failed mode
  is not eliminated.
- **Store corruption** (`f193`): `rebar fsck` reported ~2,929 issues (2,398 `SNAPSHOT_INCONSISTENT`,
  530 `ORPHAN_EVENT`, 1 `MISSING_CREATE`). Proven root cause: the Jira reconciler CI workflows
  fetched the tickets branch **shallow** (`--depth=1`); `flock` is local-only and never covered
  those cross-machine paths.
- **Not-a-bug classes the sweep had to discount**, worth recording because they consume triage:
  Gerrit Verified-gate failures are **patchset-scoped** (the gate checks out the patchset, not
  `main`) — 20 in-window failing runs were of this kind; two Release failures were
  `400 File already exists` from an operator re-dispatch; one Dependabot infra step.

## Source 3 — operator-environment traps across all 400 logs

| mechanism | logs |
|---|---|
| ambient venv / `PATH` missing `mypy`/`ruff`/`pytest`/`timeout` | 30 |
| shallow-clone `--depth=1` in a workflow | 13 |
| `git index.lock` / stale lock contention | 12 |
| editable-install assumption in a test | 11 |
| pack-size limit | 4 |
| `setup-uv` manifest | 4 |
| network-forbidden but unmocked | 3 |
| Windows `fcntl` | 3 |
| lower-bound dependency floor | 2 |
| `FORCE_COLOR=3` exported in the operator shell (fails 21 CLI-help tests on ANSI codes; 121 pass unset) | 1 |
| stale **global** `rebar` install / `anthropic-1.2.0` venv artefact | 1 |

## Classes, and what a doctor check can actually catch

| # | class | members | doctor-catchable? |
|---|---|---|---|
| C1 | **Provider transient** — 529, 520, `ModelAPIError`, `EndpointConnectionError` | 25 of 53 records | **No.** External. A doctor can only report a last-N degrade rate. |
| C2 | **Credential / entitlement** — 400 credit balance, 401 invalid key, 403 AWS AccessDenied, unresolvable auth | 7 records + 4 log hits | **Yes, cleanly.** A pre-flight token probe per configured provider catches all of these *before* a billable gate starts. |
| C3 | **Missing or wrong local install** — `[agents]` extra absent, stale global `rebar`, `anthropic-1.2.0` artefact, ambient `PATH` without `mypy`/`ruff`/`pytest`/`timeout` | 8 records + 31 log hits | **Yes — highest value.** Deterministic, local, and the most frequent operator-side class. |
| C4 | **Ambient environment poisoning** — `FORCE_COLOR=3` and peers | 1 | **Yes,** trivially: an env allow/deny scan. |
| C5 | **Host resource exhaustion** — root disk 88–98%, `/var/gerrit` 94%, git pack 481→490 MiB against a 100 MiB CI limit | 5 + 4 log hits | **Yes** for the local half (free space, `.git` size, tracker size). The CI-side pack limit needs a CI read. |
| C6 | **CI-runner / toolchain floor** — TF 1.10.5 < 1.11, Windows `fcntl`, py3.14 unmocked network, hatchling floor, non-editable install assumption, `setup-uv` manifest | 6 distinct | **Partly.** A doctor check runs on the dev box, not the runner. This is the portability boundary: it wants a matrix pre-flight, not a doctor check. |
| C7 | **Concurrency / distributed store** — shallow-clone reconcile → 2,929 fsck issues, `index.lock`, auto-push race | 13 + 12 log hits | **Partly.** `rebar fsck` already detects the *result*; detecting the *cause* (a shallow tickets-branch fetch) is a workflow lint. |
| C8 | **Service lifecycle** — the review-bot zombie: socket closed, process `Up`, 22 minutes of dropped webhooks | 1, high impact | **Yes** — a liveness probe that *binds* rather than trusting container status. |
| C9 | **Agent-loop defect masquerading as environment** — tool max-retries-of-1, `DDGSException` "No results found", and checks that can never fire (`grep -qE "^(FAILED\|ERROR)"` against ANSI-prefixed output; `gh run view --log` empty while the run is still in progress) | 11 records + a recurring-defect section in one sweep | **No** — these are harness code defects. Worth separating *because* they get filed as environmental. |

## Disposition feeding the doctor-checks scope

**Recommended as one pre-flight, in priority order: C3 + C2 + C4.** Verify the worktree venv is on
`PATH` with `mypy`/`ruff`/`pytest` resolvable; verify the running `rebar` is the checkout's build
and not a stale global; probe each configured provider's credential; warn on behaviour-changing
exported environment variables. On this corpus that is **30 + 11 + 1 = 42 of ~90** identified
incidents, all caught before a billable gate begins.

C5's local half is a cheap addition to the same check. C8 is a separate, high-value probe. C6 and
C7 are out of a doctor check's reach by construction and belong to CI-matrix and workflow-lint
work respectively. C1 is irreducible. C9 should be **reclassified out of "environment" entirely**
so it stops absorbing triage attention that belongs to the harness.

## Gaps

1. `Verified-1` root causes are not machine-readable (see the sibling CI read), so the
   environmental fraction of CI failures is inferred from narration, not measured.
2. The `CI_RESULT` JSON convention would have given a labelled `failure_type` taxonomy, but only
   9 records exist across all 400 logs — one session's convention, not a corpus.
3. Counts are per-log, not per-incident: a session that hit the same trap three times is counted
   once, so these are **lower bounds** on frequency.
