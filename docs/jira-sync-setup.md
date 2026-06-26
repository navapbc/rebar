# Automating Jira ⇄ rebar sync (GitHub Actions)

rebar's reconciler keeps a Jira project and the rebar `tickets` store in sync. Run
by hand it is `rebar reconcile`; this guide shows how to run it **automatically and
observably** in GitHub Actions, the way this repo does, so a client project can
stand the same thing up by copying two workflow files and setting a handful of repo
variables/secrets.

Two workflows do the job — they are designed to be copied **verbatim**; everything
project-specific lives in GitHub repo Variables/Secrets, not in the files:

| Workflow | File | Cadence | Purpose |
|----------|------|---------|---------|
| **Reconcile Bridge** | `.github/workflows/reconcile-bridge.yml` | every 20 min | runs `rebar reconcile --mode live`, commits the resulting events back to the `tickets` branch, and pushes |
| **Reconciler Heartbeat Canary** | `.github/workflows/reconcile-bridge-canary.yml` | hourly | files a rebar bug ticket if the bridge goes stale, and auto-closes it on recovery |

> The pair is **sufficient** for an automated, durable, bidirectional sync and
> **necessary** for it to be *reliable* unattended (the canary is the dead-man's
> switch). The deeper validation — why every step exists — is in
> [§ Necessary & sufficient](#necessary--sufficient) below. A third, optional
> `weekly-bridge-fsck` audit is described in [§ Optional hardening](#optional-hardening).

---

## 1. Prerequisites

- **A Jira project** to mirror into (its key, e.g. `REB`), reachable from the
  Atlassian CLI (`acli`).
- **A Jira identity + API token** the bridge authenticates as — ideally a dedicated
  service account, not a person. Generate the token at
  <https://id.atlassian.com/manage-profile/security/api-tokens>.
- **The `tickets` orphan branch pushed to `origin`.** `rebar init` creates the
  `.tickets-tracker` worktree on the `tickets` branch and the first write pushes it;
  confirm `git ls-remote origin tickets` returns a sha. The workflows mount this
  branch — they do not create it.
- **A pinned `acli` version + its sha256** (see step 3). Never run `latest` in CI.

## 2. Configure the sync target in `rebar.toml`

Add the `[jira]` section so local `rebar reconcile` and the CI run agree on the
target. The **secret** `JIRA_API_TOKEN` is **never** a config key — it is supplied
via the environment only.

```toml
[jira]
url     = "https://your-site.atlassian.net"   # env JIRA_URL
user    = "bridge-bot@your-org.com"           # env JIRA_USER
project = "REB"                               # env JIRA_PROJECT
```

In CI the same three values come from repo **Variables** (and the token from a
**Secret**); the env overrides the file, so CI and local stay consistent.

## 3. Set the GitHub repo Variables and Secret

Variables (`Settings → Secrets and variables → Actions → Variables`), or via `gh`:

```sh
gh variable set JIRA_URL        --body "https://your-site.atlassian.net"
gh variable set JIRA_USER       --body "bridge-bot@your-org.com"
gh variable set JIRA_PROJECT    --body "REB"

# Pin acli + verify its digest. Download once, compute the sha, then pin both:
#   curl -sSL "https://acli.atlassian.com/linux/<VER>/acli_<VER>_linux_amd64.tar.gz" | sha256sum
gh variable set ACLI_VERSION    --body "1.3.19-stable"
gh variable set ACLI_SHA256     --body "<sha256-of-that-tarball>"

# Optional — the bridge bot's git commit identity (defaults shown):
gh variable set BRIDGE_BOT_NAME  --body "rebar-bridge[bot]"
gh variable set BRIDGE_BOT_EMAIL --body "rebar-bridge@users.noreply.github.com"

# Optional — a distinct author stamp on reconciler-written events (default "reconciler"):
gh variable set REBAR_ENV_ID     --body "reconciler"
```

Secret (the only secret needed — `GITHUB_TOKEN` is provided automatically):

```sh
gh secret set JIRA_API_TOKEN   # paste the token when prompted
```

### Reference — every input

| Name | Kind | Required | Default | Used by |
|------|------|----------|---------|---------|
| `JIRA_URL` | Variable | ✅ | — | both (acli auth, reconcile) |
| `JIRA_USER` | Variable | ✅ | — | both (acli auth, reconcile) |
| `JIRA_PROJECT` | Variable | ✅ | reconciler falls back to `DIG` on create | reconcile |
| `JIRA_API_TOKEN` | **Secret** | ✅ | — | both (acli auth) |
| `ACLI_VERSION` | Variable | ✅ | — (must pin) | both |
| `ACLI_SHA256` | Variable | ⚠️ strongly recommended | warns if unset | both |
| `BRIDGE_BOT_NAME` | Variable | ❌ | `rebar-bridge[bot]` | both (commit identity) |
| `BRIDGE_BOT_EMAIL` | Variable | ❌ | `rebar-bridge@users.noreply.github.com` | both |
| `REBAR_ENV_ID` | Variable | ❌ | `reconciler` | reconcile (event author stamp) |
| `GITHUB_TOKEN` | (auto) | ✅ | provided | canary (Actions API), both (push) |

## 4. Copy the workflows

Copy both files from this repo into your `.github/workflows/`. The **only** edit a
client makes is the install step — this repo installs itself from source:

```yaml
      - name: Install rebar
        run: |
          python -m pip install --upgrade pip
          pip install .            # <-- clients: pip install nava-rebar==<pinned>
```

Pin a released version (`pip install nava-rebar==X.Y.Z`) so the reconciler code is
reproducible across runs. Reconcile does **not** need the `[agents]` extra.

## 5. Validate safely, then enable

Do **not** let `live` mode be your first run. The bridge supports read-only and
no-write modes for exactly this:

1. **`reconcile-check`** — dispatch *Reconcile Bridge* manually with
   `mode = reconcile-check`. Read-only diagnostic: no lock, no writes, no Jira
   mutations. Confirms creds, acli, and worktree mounting work.
2. **`dry-run`** — computes the full mutation plan and applies nothing. Review the
   plan in the run log.
3. **`live`** — enable the schedule. The first live run may be large (it reconciles
   the whole backlog): it creates one Jira issue per local ticket **serially** via
   acli (~4 s each), so the job's `timeout-minutes` must cover the full initial
   pass — commit-back only persists on a **completed** pass, so a pass that times
   out makes no durable progress and the next pass re-does the work. The shipped
   `reconcile-bridge.yml` sets `timeout-minutes: 60` (a ceiling for the one-time
   bulk sync; steady-state incremental passes finish in minutes). Raise it if your
   backlog is larger than a few hundred tickets.
4. Watch the **canary**: dispatch it with `dry_run = true` to see the staleness
   readout without filing a ticket.

> **What to expect on the first bulk sync.** The reconciler creates issues first,
> then later passes sync mutable fields (status, parent, links). Over-length
> **descriptions** are truncated automatically to fit Jira's limit (the limit is on
> the ADF representation, not the plain text) with a `[truncated by reconciler]`
> marker — the local store keeps the full text. A local **assignee** that is not a
> Jira user cannot be set and is skipped (soft-fail); the pass still succeeds.

---

## Necessary & sufficient

Each workflow step maps to a concrete fact about rebar's reconciler. This is the
validation that the two workflows are **necessary** (nothing here is removable
without breaking durable sync) and **sufficient** (nothing else is required).

### Reconcile Bridge — why each step exists

| Step | rebar fact that requires it |
|------|------------------------------|
| **Mount `tickets` as a worktree** | The store lives on the `tickets` orphan branch at the repo root; `actions/checkout` lands you on `main`. The reconciler reads/writes `.tickets-tracker`, so the branch must be mounted there. We mount on the real `tickets` branch (`-B tickets`) so `tracker.branch` matches and `rebar fsck` doesn't WARN. |
| **`rebar reconcile --mode <mode>`** | This is the reconcile entry point (`== python -m rebar_reconciler`). It is a lean-runtime capability — no `[agents]` extra needed. |
| **Exit-code handling (0 / 75 / 3 / other)** | `__main__.py` returns **75** (reschedule — rebase-retry exhausted; the next */20 run retries) and **3** (another pass already holds the pass-lock). Both are operational, not errors, so we exit 0 on them; any other non-zero fails the job. |
| **Commit-back + push when dirty *or ahead*** | **The reconciler does not push.** It writes inbound events as *uncommitted* files in the worktree and makes its own `.bridge_state/bindings.json` commit *without pushing*. So a clean worktree does **not** mean "nothing to push" — we push whenever the local `tickets` branch is ahead of `origin/tickets`. This is the single biggest divergence from a naive DSO copy (whose `git status --porcelain` gate would skip pushing the reconciler's own binding commit). |
| **Fetch-rebase-push retry loop** | Multiple writers (this bridge, the canary, interactive `rebar` clients) push to the same orphan branch. The event log is union-mergeable, so a rejected push is resolved by fetch→rebase→retry with backoff. |
| **`concurrency: reconcile-bridge` (cancel-in-progress: false)** | A second guard atop the reconciler's own pass-lock; ensures an in-flight pass finishes before the next scheduled one starts rather than racing it. |
| **acli download + sha256 verify + auth** | The reconciler shells out to `acli` for all Jira I/O. Pinning + checksum-verifying the binary keeps CI reproducible and supply-chain-safe. |
| **`timeout-minutes: 60`** | The one-time initial sync creates issues serially via acli (~4 s each), and commit-back persists only on a **completed** pass — so the budget must cover a full bulk pass or progress never converges. 60 is a ceiling, not a duration; steady-state passes finish in minutes. |
| **`permissions: contents: write`** | The minimum to push to `origin/tickets`. The default `GITHUB_TOKEN` suffices — no PAT. |

### Reconciler Heartbeat Canary — why each step exists

| Step | Reason |
|------|--------|
| **Query last successful `reconcile-bridge.yml` run** | A silently-disabled or chronically-failing bridge is invisible otherwise. The canary is the dead-man's switch that makes staleness loud. |
| **Treat GitHub API errors as transient** | Treating an API blip as "stale" would file a false-alarm bug every outage. |
| **`rebar list/create/comment/transition`** | rebar's CLI is the ticket interface. Unlike the reconciler, **CLI writes auto-commit and auto-push** to `origin/tickets`, so the canary needs no explicit commit-back for its ticket ops — only a best-effort *flush* guard (auto-push is non-fatal on failure). |
| **Bug-close `--reason "Fixed: …"`** | rebar enforces a bug-close reason prefixed `Fixed:` or `Escalated to user:`. Auto-recovery counts as a fix. |
| **`BRIDGE_CANARY_ALERT:` comment prefix** | The reconciler's outbound comment sync excludes this prefix, so the canary's fresh-timestamped "still stale" comments are **not** mirrored to Jira (a volatile timestamp never dedups → duplicate Jira comments). |
| **`permissions: contents: write`, `actions: read`** | Minimum: push ticket changes + read the bridge's run history. |
| **Fail the job on stale** | Surfaces the alert as a red run in the Actions UI in addition to the ticket. |

### Intentionally omitted (and why)

- **The `scripts/jira-pressure-test/` e2e probes** (`e2e_validation_probe.sh`, …) are
  explicitly **manual, live-mutating** tooling (their README says *do not wire into
  CI*). For automated validation use `mode = reconcile-check` or `rebar bridge-probe`
  instead.
- **A `BRIDGE_ENV_ID` input** (DSO required one). rebar doesn't: the reconciler stamps
  events with `REBAR_ENV_ID` (default `"reconciler"`) — it's an author label, not a
  required identity. Set it only if you want a distinct sync-bot author in the log.
- **The `[agents]` extra / `ANTHROPIC_API_KEY`.** Reconcile is a lean-runtime path; the
  LLM extra is only for `review`/`verify-completion`, not sync.

### Divergences from DSO at a glance

| Concern | DSO | rebar |
|---------|-----|-------|
| Invocation | `python -m dso_reconciler` | `rebar reconcile --mode <mode>` |
| Reschedule / lock exit codes | (n/a) | **75** reschedule, **3** pass-in-flight (handled) |
| Reconciler pushes its events? | no → commit-back required | no → commit-back required, **plus push-when-ahead** for its binding commit |
| Ticket CLI | `.claude/scripts/dso ticket …` | `rebar list/create/comment/transition` (auto-push) |
| Install | `requirements.lock` | `pip install nava-rebar==<pinned>` (clients) |
| Env identity | `BRIDGE_ENV_ID` (required) | `REBAR_ENV_ID` (optional, default `reconciler`) |

---

## Sync semantics & limitations

What round-trips, and where Jira's data model bounds it.

| Relationship / field | Local → Jira | Jira → Local |
|----------------------|:-:|:-:|
| Title, description, status, priority, assignee | ✓ | ✓ |
| Comments | ✓ | ✓ |
| Labels ↔ tags | ✓ | ✓ |
| Issue links (`blocks`/`depends_on` ↔ Blocks, `relates_to` ↔ Relates) | ✓ | ✓ |
| Parent — **only when the parent is an Epic** | ✓ | ✓ |
| Parent — when the parent is a non-Epic (Story/Task/Bug) | **excluded** | n/a |

### The parent-hierarchy limitation (multi-level trees do not fully round-trip)

rebar's local hierarchy (`parent_id`) supports **arbitrary depth** —
`epic → story → task → …`. Jira's hierarchy does **not**: on a standard project
the only parent edge between issue types is **Epic → (Story/Task/Bug)**, with
sub-tasks as the one level below. A Story (or Task) **cannot** be the parent of a
Task — Jira rejects it with `HTTP 400`.

Because of this, the reconciler **only syncs a parent edge whose parent is an
Epic** (ticket `8b25`; `outbound_differ._map_local_to_jira_fields` suppresses a
parent diff when the resolved parent's `ticket_type != "epic"`, and logs the
exclusion). Concretely, for a local chain `epic E → story M → task L`:

- `M.parent = E` (parent is an Epic) **syncs** in both directions.
- `L.parent = M` (parent is a Story) is **not synced** — it is suppressed
  outbound (and Jira would reject it anyway). In Jira, `L` appears **unparented**.
- The **full chain is always preserved in the local store** — only the Jira
  projection is flattened to its Epic-parent edges.

So a deep local tree shows up in Jira as a set of Epic→child edges, with the
deeper (non-Epic-parented) levels simply absent on the Jira side. This is
consistent and non-destructive: no churn, and the local hierarchy is never
altered by the exclusion.

### Why the deeper edge is dropped, not "promoted to the nearest Epic"

A tempting fix is to promote `L`'s parent to its nearest **Epic** ancestor (`E`)
on outbound, so `L` at least rolls up under the Epic in Jira. We deliberately do
**not** do this, because it breaks the inbound direction: once Jira shows
`L.parent = E`, the next inbound pass would mirror that back and **overwrite the
real local parent** `L.parent = M` with `E` — silently corrupting the local
hierarchy (and then oscillating against the outbound exclusion every pass). Since
parent sync is bidirectional, any outbound parent we write must be a parent we are
willing to accept back inbound — and `E` is not the true local parent of `L`.
Dropping the un-representable edge keeps the two directions consistent; promoting
it would not. (If Jira-side roll-up is ever needed, it must be carried by a
mechanism that is **not** mirrored back as `parent_id` — e.g. a separate label or
a one-way projection — not by writing a false parent.)

---

## Rollback / disable

- **Pause sync:** disable *Reconcile Bridge* in the Actions UI (or delete its
  `schedule:` trigger). The canary will then alert on staleness — pause it too if the
  pause is intentional, or close its alert ticket manually.
- **Safe re-validate:** before re-enabling `live`, dispatch with
  `mode = reconcile-check` then `dry-run`.
- **Bad push:** the `tickets` branch is ordinary git history — an erroneous reconciler
  commit on `origin/tickets` is revertable like any other commit (the event log is a
  union merge, so reverts converge across clones).

## Re-targeting an existing store to a new project

Changing `[jira] project` only governs where **new, unbound** tickets are created.
A store that previously synced to another project keeps that project's **bindings**
(`.bridge_state/bindings.json`) and a stale remote snapshot
(`.bridge_state/prev_snapshot.json`), so the reconciler keeps targeting the old
project's issues. Since the cross-project guard (bug 626d) the outbound applier
**refuses** such writes (fail-closed) — so to actually move to the new project you
must clear that legacy bind-state and let every local ticket re-create fresh.

Use the migration tool (dry-run by default; writes nothing until `--apply`):

```sh
# 1. report what would change (no writes):
python scripts/retarget_jira_project.py --tracker-dir .tickets-tracker

# 2. apply — clears bindings.json + prev_snapshot.json (backs them up first);
#    add --strip-tags to also remove residual dso-id:jira-<old>-* id tags:
python scripts/retarget_jira_project.py --tracker-dir .tickets-tracker --apply

# 3. VERIFY before enabling live sync — the plan must show 0 old-project targets:
rebar reconcile --mode dry-run
```

This was validated on a clone of a DIG-bound store: clearing bindings +
`prev_snapshot.json` dropped the dry-run plan from **1415 mutations (1017 targeting
DIG)** to **398 clean outbound creates with 0 DIG targets** — i.e. every active
ticket re-creates fresh in the new project. **A live pass after this bulk-creates
one new issue per active local ticket**, so run it deliberately (off-cadence; pause
the schedule), and commit the cleared bind-state back to the `tickets` branch.

## Optional hardening

DSO also ships a **weekly bridge-fsck audit**. rebar exposes the same check as
`rebar bridge-fsck` (orphaned mappings, duplicate Jira keys, stale SYNCs). To add it,
create a third workflow that mounts the `tickets` worktree (steps 1–2 above) and runs:

```yaml
      - name: Bridge fsck audit
        run: rebar bridge-fsck --tickets-tracker=.tickets-tracker --output json
```

on a weekly `cron` (e.g. `0 6 * * 1`), failing the job on anomalies so they surface
before they accumulate. It is *hardening*, not required for sync.
