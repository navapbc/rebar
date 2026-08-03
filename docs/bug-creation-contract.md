# The bug-creation contract for automated filers

Any automation that files bug tickets — CI canaries, the Jira reconciler, future
watchdogs — MUST follow this contract. It exists so automated filers stay
high-signal: no duplicate floods, no hollow tickets, no unattributed creates.
It was extracted from the audit of the three existing auto-file sites (ticket
`4527-0cfa-d31a-4a08`); those three are the reference implementations:

| Filer | Code | detected_by |
|---|---|---|
| Heartbeat canary | `scripts/canary_bridge.py` `cmd_heartbeat_alert` | `heartbeat-canary` |
| Binding-drift canary | `scripts/canary_bridge.py` `cmd_binding_drift_alert` | `binding-drift-canary` |
| Reconciler conflict filer | `src/rebar/_engine/rebar_reconciler/conflict_bug_filing.py` | `reconciler-conflict` |

## The five elements

### 1. Dedup search FIRST

Before creating, search for an open bug already representing the same failure
condition, keyed by a **stable tag**:

```sh
rebar list --type=bug --status=open --has-tag=<tag> --output json
```

- A **singleton condition** (there can only be one instance of the failure,
  e.g. "the reconciler heartbeat is stale") uses a fixed tag
  (`heartbeat-alert`, `binding-drift-alert`).
- A **per-instance condition** derives the tag from the instance identity —
  the conflict filer uses `conflict-<sha1(local_id + NUL + jira_key)[:12]>`
  (`conflict_bug_filing.conflict_dedup_tag`).

If an open ticket is found, the repeat observation is **absorbed** into it
(see accumulation) — never create a second ticket for the same condition.
Dedup-search failure fails toward **creating** (a possible duplicate beats a
silently swallowed defect).

### 2. Accumulation cap (≤ 1 comment / 24h)

An absorbed repeat posts a marker comment on the open ticket **at most once
per 24h window**, so a condition that persists across many runs cannot flood
the ticket. Recipe: `rebar show <tid> --output json`, scan `comments[]` for a
body starting with the filer's **marker prefix**; skip commenting if the
newest such comment is younger than 24h. Store timestamps are nanoseconds —
normalize (`ts > 1e12 ⇒ ts / 1e9`).

Marker convention: a short SCREAMING prefix ending in a colon, stable per
filer family — `BRIDGE_CANARY_ALERT:` (both canaries), `RECONCILER_CONFLICT:`
(conflict filer). The marker makes accumulation comments machine-findable and
is what the 24h dedup keys on. Fail-soft toward commenting: a duplicate
comment is cheaper than a silent gap.

### 3. Abort-if-empty (no hollow tickets)

If the filer cannot populate the fields a responder needs — empty status
detail, empty title/description, no failure identifiers — it must **refuse
loudly** rather than file a hollow ticket:

- CI-step filers print `::error::` and exit **non-zero** (the canary run goes
  red — the wiring bug is itself a defect to surface).
- Library/best-effort filers (the conflict filer, which must never fail the
  reconciler pass) print to stderr and return `""`.

An empty-detail abort means the *filer's inputs* are broken; fix the wiring,
don't relax the guard.

### 4. Flake threshold — for point-in-time probes only

A probe that samples a condition which can transiently self-heal (an API
blip, a runner hiccup) must require **2 consecutive red observations** before
filing. The heartbeat canary queries its own workflow run history — no new
state store:

```sh
gh api "repos/$GITHUB_REPOSITORY/actions/workflows/$CANARY_WORKFLOW_FILE/runs?status=completed&per_page=5" \
  --jq '[.workflow_runs[] | {id, conclusion, updated_at}]'
```

Exclude the current `GITHUB_RUN_ID`; the most recent remaining completed run
must be a `failure` for the threshold to be met. Query failure fails toward
**not filing** (loud `::warning::`, exit 0 — the next cron cycle retries).
The step needs `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` and the workflow's own
filename in `CANARY_WORKFLOW_FILE`.

**Divergence rationale — binding drift has NO threshold:** drift is persistent
store state that cannot self-heal between runs, and the fsck oracle never
fails the canary run, so run conclusions carry no drift signal to count. A
first observation files immediately. Apply the same reasoning to new filers:
threshold point-in-time probes, file persistent-state findings at once.

### 5. Provenance (`detected_by`)

Every automated create stamps its detection channel:

```sh
rebar create bug "<title>" --description "<desc>" --tags <tag> --detected-by <channel>
```

`--detected-by` overrides the `REBAR_DETECTED_BY` env var (either works; the
flag is explicit and survives environment refactors). Channel names are
lowercase, hyphenated, and name the *detector*, not the failure. This feeds
the detected-by taxonomy used by `rebar metrics` (escape-rate lenses).

## Ticket content

The create must give a responder enough to act without re-deriving state:
reproduction pointer (workflow file / command), expected vs actual, first-red
and detected-at timestamps, the triggering run URL, and acceptance criteria.
See `_heartbeat_description` / `_drift_description` in
`scripts/canary_bridge.py` for the shape.

**Auto-close divergence:** the two canary filers auto-close their ticket when
the condition recovers (`transition … --force-close=…`), because a
bot-observed recovery is the ticket's entire acceptance criterion. The
conflict filer does NOT auto-close — resolving a reconciler conflict requires
human/agent adjudication, so its tickets close through the normal gated flow.
New filers must pick a side deliberately: auto-close only when recovery is
fully machine-verifiable AND the ticket has no other acceptance criteria.

## Exit codes (CI-step filers)

| Situation | Exit |
|---|---|
| Filed / absorbed / no-op (green, no ticket) | 0 |
| First red below threshold; or history-query failure | 0 (loud warning) |
| Empty-detail abort (wiring bug) | 1 |
| rebar CLI write failed | the CLI's non-zero rc |
