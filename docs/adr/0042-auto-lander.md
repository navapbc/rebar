# ADR 0042 — Parallel-agent auto-lander for `main` (serial Phase 1)

> **Superseded by [ADR 0047](0047-retire-autolander-rebase-if-necessary.md) (2026-07-13).** Kept for historical context; the decision below no longer reflects how changes land.

**Status:** Accepted
**Date:** 2026-07-12
**Builds on:** ADR 0040 (Fast Forward Only submit type — the R5 backstop this lander sits on),
ADR 0041 (LLM-Review carries `TRIVIAL_REBASE` — a conflict-free rebase re-runs *only* CI, not
the LLM review), ADR 0020 (two-vote CI gate), ADR 0025 (feature-branch merge-carry).

This ADR is the durable *rationale* record for the serial auto-lander (epic `f1fa-57d8-07cb-44af`,
alias `peridotite-thickset-pintail`). The stable, versioned *interface* it anchors — the `land` /
`land-status` outcome enum and exit codes — was `docs/land-contract.md`
(`contract-version: 1`); this ADR cited it rather than restating it. (That contract doc,
and the auto-lander itself, were removed by ADR 0047.)

## Context

ADR 0040 made `main` **Fast Forward Only**: a change is submittable only when it sits directly on
the current `main` tip, and when `main` advances under an in-review change that change goes
**non-submittable** until it is rebased (or re-merged) onto the new tip. That rebase mints a new
patch set that drops `Verified` and re-runs CI on the exact tree that will land — the mechanism
that makes it *impossible to land a stale or untested tree* (ADR 0040's requirement R5).

FFO delivered R5 but left R4 (graceful parallel landing) as an explicit accepted cost: **a manual
rebase-treadmill.** In practice, parallel agent sessions race to land on `main`; the loser of each
race must hand-roll `git fetch && git rebase origin/main`, re-push, poll for a fresh `Verified`,
and retry — and lose again if another change lands first. With a multi-story feature **stack** this
is worse: every rebase-to-tip forces a CI re-run on non-conflicting changes. The cost is real,
live, and active:

- **Wasted wall-clock** — redundant CI runs on changes that never conflicted (GitHub Actions
  minutes are free for this OSS repo, but the runner concurrency is capped, so redundant runs
  serialize behind each other and slow *everyone's* landings).
- **Wasted agent tokens/attention** — a session that could have walked away instead sits in a
  poll-rebase-retry loop, correlating Gerrit votes/labels/submittability by hand.

There is also a **correctness trap in the naive mental model**: agents were taught to "watch the
two votes; once `LLM-Review +1` and `Verified +1`, it will land." Under FFO that is **false** — a
change can carry both green votes and still be **non-submittable** because `main` advanced beneath
it (a behind-tip change is not a descendant of the tip). "Both votes green" no longer implies "it
will land," and an agent that assumes it does silently reintroduces babysitting exactly when a
conflict/TOCTOU race occurs.

We want agents to set an opt-in intent and **walk away**: changes land autonomously in the common
case, or are handed back with **one typed, actionable outcome** — without ever weakening the
two-vote gate. A full speculative merge queue (Zuul) is the throughput-preserving answer but was
rejected in ADR 0040 as cost-disproportionate at ~26 merges/day (it cannot use GitHub Actions as
its runner; it demands a ZooKeeper + SQL + scheduler + executor + Nodepool control plane). This ADR
takes the lightweight-bot path ADR 0040 anticipated ("a lightweight Gerrit commit-queue bot before
Zuul").

## Decision

Build a **single-instance serial auto-lander** — **this project's infrastructure** (Gerrit +
GitHub-Actions specific), living under `infra/autolander/` and deployed like the review-bot. Its
core loop:

1. **Select** the front `Autosubmit`+submittable change/chain (FIFO by the `Autosubmit` vote's
   approval date; `infra/autolander/loop.py::select_front_candidate`).
2. **Rebase to the current tip** preserving the uploader
   (`POST /changes/{id}/rebase` or `/rebase:chain`, `RebaseInput.rebase_on_behalf_of_uploader=true`).
   The rebase drops `Verified` (copyCondition `NO_CODE_CHANGE` only, ADR 0040) so CI re-runs on the
   integrated tree; on a conflict-free rebase `LLM-Review` carries (ADR 0041).
3. **Await a fresh `Verified +1` on every rebased member** (`await_fresh_verified`), recording each
   member's CI-tested SHA.
4. **Ancestor-atomic submit** the exact tested SHAs with one `POST /changes/{tip}/submit`
   (Gerrit "Submitted Together" by relation-chain ancestry, reinforced by a shared topic +
   `change.submitWholeTopic`).

On any failure the **whole stack is handed back to its owning agent** — never partial-land, never
evict a change from within a stack.

**The agent contract is one call → one typed terminal outcome.** Agents drive landing through the
project `land` / `land-status` command, which returns exactly one outcome from a closed enum —
`merged | needs_rebase | ci_failed | review_failed | not_requested | abandoned | lander_down |
error | pending | timed_out` — each with a pinned exit code. Agents depend **only** on this
contract; they never correlate Gerrit votes/labels/submittability themselves. The "both votes green
but conflicts" trap is resolved *inside* the tool. The full enum, exit codes, and JSON schema were
in `docs/land-contract.md` (`contract-version: 1`), removed with the auto-lander by ADR 0047.

**Outcome provenance (the deliberate split model).** The one outcome that is *not* a native Gerrit
fact — `needs_rebase` (a rebase conflict / not-fast-forward-landable state, which has no live
Gerrit signal) — is **recorded by the lander into its own state** (`infra/autolander/failure.py`,
a self-invalidating marker keyed by change_id + patchset SHA) and read back by `land`. The three
outcomes that **are** native facts — `merged` (status MERGED), `ci_failed` (post-rebase
`Verified -1`), `review_failed` (`LLM-Review -1`) — are **derived live** by `land` from Gerrit
primitives, not stored. So `land` is the single *reporter* that composes one unified typed outcome
from one bot-state read plus live Gerrit reads.

## The R5 safety invariant — the tested tree *is* the merged tree

R5 (nothing lands whose to-be-merged tree wasn't CI-tested) is the load-bearing guarantee, and the
lander is built so that automation **cannot** weaken it. Each mechanism that preserves it:

- **FFO backstop (ADR 0040).** Submit is refused unless the change is a descendant of the current
  `main` tip. Even if every other guard failed, Gerrit itself will not fast-forward a stale tree.
- **Fresh `Verified` per rebased member on its own SHA.** After the rebase drops `Verified`, the
  lander awaits a *new* `+1`; because the vote was dropped and re-cast, a present `Verified +1` is
  by construction on the post-rebase SHA — never a copied/carried vote
  (`has_fresh_verified` / `all_members_fresh_verified`). For a multi-member chain **every** member
  must carry its own fresh `Verified` before submit.
- **The bot casts no votes.** The lander only *reads* `Verified`/`LLM-Review` and *sets/removes*
  the non-gating `Autosubmit` label. It never direct-casts a gate vote, so it cannot manufacture a
  green tree (the GerriScary posture — see Security invariants).
- **FFO TOCTOU re-check immediately before submit** (`is_landable`). `submittable=True` is only
  true as of the read; between the read and the `POST /submit`, `main` can advance. Just before
  submitting, the lander re-confirms that *every* member is still `submittable` **and** its current
  revision SHA is unchanged from the recorded tested SHA. Any drift → not landable → re-drive or
  hand back. A residual `not fast-forward` submit refusal is also caught and treated as
  not-landable.
- **Partial-land detection → loud failure + hand-back.** After the atomic submit, the lander
  verifies **every** member reached `MERGED`; if any is still open it emits a structured
  `AUTOLANDER_ERROR` line (the observability alarm keys off this token) + a metric, hands the stack
  back, and raises `PartialLandError` rather than proceeding on a partial land (`ancestor_atomic_submit`).

This is the same gating guarantee Zuul / Chromium-CQ provide: automation may *sequence* landings,
but the thing that merges is always a tree that CI verified in the state it will land.

## The eight key design decisions (with prior-art grounding)

Every decision emulates a proven, actively-maintained OSS workflow rather than inventing one
(ADR 0040's R6). Prior art: **Zuul**, **LUCI CV / Chromium CQ**, **Gerrit REST primitives**,
**Prow / Tide**.

1. **Intent = an `Autosubmit` Gerrit label, requester-votable by `Contributors`.** An agent sets
   the label under its **own** identity (the bot never votes it); it is the lease the agent hands
   the bot, and it coexists with manual submit. **Analogue:** Chromium CQ's `Commit-Queue +2`
   opt-in. It is **non-gating** (informational — gating would be redundant under FFO) and
   **non-sticky** (empty copyCondition), so it never contributes to the merge decision.

2. **Two-channel status surface.** The per-change outcome is reported **back onto the Gerrit
   change** (a comment + the `Autosubmit` label state), which `land` reads via the Gerrit
   credentials it already holds — the CQ pattern. **Separately**, a **read-only HTTP status
   endpoint** exposes bot liveness (`heartbeat_age_s`) and queue state, the Zuul `zuul-web` /
   Prow-Tide `deck` pattern. This resolves the "file vs metric" ambiguity: a local heartbeat file
   backs the in-container HEALTHCHECK, while the status endpoint surfaces liveness to a remote
   `land` invocation. The status server listens on the container's fixed port 8080, published on
   the host loopback as **8081** (`AUTOLANDER_PORT`) — 8080 on the host is Gerrit's — and nginx
   routes `GET /autolander/status` to it.

3. **CQ-reset hand-back.** On a rebase conflict, a post-rebase `Verified -1`, or exhaustion of the
   bounded re-drive, the lander **removes `Autosubmit` from every stack member + posts a comment
   naming the manual step + writes the `needs_rebase` marker** (`hand_back` /
   `remove_autosubmit_from_stack`). The label-removal is the "handed back" signal `land` detects.
   **Analogue:** Chromium CQ "reset" / Zuul dequeue-and-comment. This concretizes the stack as the
   atomic unit for *failure* as well as landing — never partial, never evict-from-within.

4. **Raw time-to-land timer/metric.** Emit a **raw** `autolander_time_to_land_seconds` timer
   (individual datapoints); percentiles/buckets are computed **downstream** (CloudWatch extended
   statistics), never hard-coded in the emitter. **Analogue:** Zuul's `resident_time`. (An earlier
   draft that pinned fixed histogram buckets was dropped — CloudWatch has no native
   histogram-with-buckets, and downstream extended statistics are the idiomatic fit.)

5. **Autoheal hang-recovery.** Plain docker-compose has no Kubernetes liveness probe, so a
   container **HEALTHCHECK** marks the container unhealthy on a **stale local heartbeat** (it
   probes the *local* heartbeat file, never Gerrit, to avoid flapping on a transient Gerrit
   hiccup), and the **`willfarrell/autoheal`** sidecar (watching Docker health state, Docker socket
   mounted **read-write** — autoheal issues `docker restart`, a daemon write) restarts the
   unhealthy container. **Analogue:** the K8s liveness-probe pattern, adapted to plain compose.
   Accepted new stack dependency + Docker-socket attack-surface item for the deploy review.

6. **Stdlib Gerrit client (not `pygerrit2`, not a `src/rebar` import).** The Gerrit helper reuses
   the review-bot's proven **stdlib-only** pattern — `urllib` + HTTP Basic auth + XSSI-prefix
   stripping — as a thin helper in `infra/autolander/`. It deliberately does **not** add
   `pygerrit2` (a dependency the repo avoids) and does **not** import `src/rebar` (which would put
   Gerrit-specific landing logic in platform-agnostic core). **Grounding:** peers (Zuul drivers,
   LUCI/CV vendoring go-gerrit) extract-and-share eventually — which *is* the deferred provider-seam
   (idea `1701`) — but the existing `src/rebar/review_bot/gerrit_client.py` has no rebase/submit
   surface and is not a drop-in, and a production review-bot refactor mid-feature is out-of-scope
   risk. The thin stdlib helper is the low-risk fit; it reverses cleanly to a shared-module
   extraction when the seam is built.

7. **`rebase_on_behalf_of_uploader`.** The rebase preserves the original uploader (and therefore
   the `Signed-off-by`/DCO and `rebar-ticket` trailers) and forces CI to re-verify the integrated
   tree. **Grounding:** LUCI CV / go-gerrit "rebase on behalf of uploader." The `Rebase` access
   right is a per-ref ACL on the rebar project's `refs/heads/*` for `Contributors`; the on-behalf
   half needs no extra grant (it checks the original uploader's existing push right).

8. **Emergency-stop sentinel + bounded SIGTERM drain.** A **state-volume sentinel file**, checked
   each loop, moves the bot to a `paused` phase (stop consuming new work). On SIGTERM the bot
   **writes a crash-safe recovery record as its first action**, keeps heartbeating during the drain
   (so autoheal is not provoked into a mid-drain `docker restart`), finishes the in-flight
   `wipChain`, writes its outcome, and exits within a bounded drain window (matched by compose
   `stop_grace_period`, since Docker's 10 s default would SIGKILL before the record is written).
   **Analogue:** Zuul `pause` / `graceful`. On restart the recovery record is reconciled against
   live Gerrit + the `needs_rebase` markers so there is no double-submit and no stranded stack.

## Security invariants

The load-bearing security property is that **`Autosubmit` cannot weaken the two-vote
(`LLM-Review +1` AND `Verified +1`) gate** — the GerriScary / CVE-2025-1568 posture (an attacker
must not be able to land an unreviewed/untested tree by manipulating a non-gate label or racing a
vote-copy). Each invariant and the concrete check that proves it:

| Invariant | Concrete check |
|-----------|----------------|
| `Autosubmit` is **non-gating** (informational only; gating is redundant under FFO). | `grep` of `infra/gerrit/project.config`: the `Autosubmit` label carries no submit requirement (it is not in any `submittableIf`/`submit-requirement`), empty `copyCondition`. |
| `Autosubmit` is **requester-votable but tightly scoped** — granted to group `Contributors` only, so an agent sets it under its own identity; the bot casts no votes. | `GET /a/projects/rebar/access` diff (below) shows the vote grant is exactly `label-Autosubmit` on `refs/heads/*` for `Contributors` + `Administrators`; loop/land code casts no gate vote (`grep` the client for `set_review(... "Verified"|"LLM-Review")` → only reads). |
| The bot **re-verifies `is:submittable` on the exact tested SHA immediately before submit**, so a stale/racing tree cannot slip through. | Unit test asserting `is_landable` returns false when any member's current SHA ≠ the recorded tested SHA, or when `submittable` is not `True`; plus the residual `not fast-forward` submit-refusal path. |
| The **two-vote gate is unweakened** (no direct `Verified`/`LLM-Review` cast). | The lander only reads gate votes and only writes `Autosubmit`; no code path casts a gate label. |

**The concrete access-diff check.** Before the `Autosubmit` label was pushed to `refs/meta/config`,
S1 captured the live `GET /a/projects/rebar/access` response to
**`infra/gerrit/access-snapshot-pre-autolander.json`** and committed it in the *same* change that
adds the label. The proof is a diff of a fresh `GET /a/projects/rebar/access` against that committed
baseline showing the ACL change is **exactly** the addition of `label-Autosubmit` on `refs/heads/*`
for `Contributors` + `Administrators` (and the `Rebase` access right for decision 7) — **and nothing
else**. Any other delta (a new gate-label grant, a widened submit ACL, a bot-cast Verified grant)
fails the check. Without the pre-cutover baseline the diff would be unperformable, which is why its
capture-before-push is an epic AC in its own right.

## Separation-of-concerns boundary

Landing / merge-queueing is **platform-specific and maintainer-owned** — it is *not* part of
rebar's platform-agnostic ticket / workflow / gate / reconcile mission. So the entire auto-lander is
**this project's infrastructure** (Gerrit + GitHub Actions): it lives under `infra/autolander/` +
the project `land` CLI, deploys like the review-bot, and adds **no Gerrit-specific landing logic to
`src/rebar` core**. The boundary is enforced at the import level: `infra/autolander/loop.py` and
`failure.py` **may `import rebar`** for ticket ops only (e.g. annotate a ticket that its change
landed); the Gerrit helper (`gerrit.py`) is **stdlib-only** and imports nothing from rebar. The
platform-agnostic generalization is deliberately deferred (idea `1701`, below).

## Reliability posture + the accepted single-instance SPOF tradeoff

The lander is a **single writer** — one instance, serialized by a `flock` so there is never a second
process rebasing onto one HEAD. Reliability rests on: the flock (single-writer), a heartbeat + the
autoheal hang-recovery (decision 5), a loud `AUTOLANDER_ERROR` metric + a stuck-state
(time-in-phase) alarm, and a crash-safe recovery record reconciled against live Gerrit + the
`needs_rebase` markers on restart (no double-submit, no stranded stack).

The single instance is an **accepted SPOF for Phase 1.** The tradeoff is deliberate and bounded: its
outage does not corrupt anything — it degrades to the *manual status quo*. When the bot's heartbeat
is stale (> 90 s) or its status endpoint is unreachable, `land` fails **fast** to `lander_down`
(exit 6) rather than hanging, and the sanctioned degraded path is the **manual FFO rebase + submit**
(CONTRIBUTING §2e, ADR 0040). So an outage is surfaced immediately and actionably, and a human/agent
can always land by hand. HA (an externalized queue/lock replacing the single instance +
in-memory `wipChain`) is a deferred promotion, below.

## Deferrals — and the triggers that promote them

Both idea ids resolve in the store and this ADR is anchored to them.

- **Platform-agnostic landing provider-seam** — idea **`1701-8bc1-e3b7-4984`**
  ("Platform-agnostic landing provider-seam in rebar core: Gerrit/GitHub/GitLab adapters + portable
  outcome enum"). A landing adapter in rebar core behind one seam that unifies the review-bot client
  and the autolander helper. **Promote when a SECOND landing platform needs to land** (YAGNI until
  then; consistent with rebar's provider-neutral identity/reconciler direction). Grounding: the
  Zuul-driver / go-gerrit-vendoring "extract-and-share once there's a second consumer" model.

- **Bounded-speculation cross-stack batching (Phase-2 throughput)** — idea
  **`c3a9-5f4c-bdc6-4e27`** ("Phase-2 throughput: bounded speculation"). Cross-stack batching was
  spiked and **rejected** on this infra (the gerrit-to-platform bridge fires CI per-patchset → N
  runs not one; landing N off one run would need a GerriScary-class direct `Verified` cast; wins
  only in rare bursts of ≥ 4, plus a bisection tax + WIP-hook fragility). The correctly-shaped
  follow-on is **bounded speculation** — speculate ≤ the GitHub-Actions concurrency cap, each
  candidate a clean single-change-on-tip verified on its own tree. **Promote ONLY on a MEASURED
  serial-treadmill pain** (not "speculation ruled out" — the shape, not the goal, was rejected).

- **HA / external state store.** Replace the single instance + in-memory `wipChain` with an
  externalized queue/lock (Zuul's scale-out lesson). **Promote if single-instance availability
  becomes insufficient** (the `lander_down` → manual-fallback degradation proves inadequate in
  practice).

## Consequences / accepted costs

- **Zero-attention common-case landing.** An agent sets `Autosubmit` (or runs `land --wait`) and
  walks away; in the non-conflicting case the change/stack lands with no manual rebase-retry and no
  vote-watching. Redundant CI runs from the parallel-landing race are eliminated (each change is
  CI'd only as needed by the serial lander).
- **One typed outcome, never vote-correlation.** On any failure the agent receives one actionable
  outcome; the "both votes green but conflicts" trap is handled inside the tool.
- **Serial throughput only (deferred).** Phase 1 lands one change/chain at a time; throughput beyond
  serial is idea `c3a9`, promoted only on measured pain.
- **New operational surface.** A new deployed service + the autoheal sidecar + a Docker-socket mount
  + a state volume + a status endpoint — reviewed as deploy/observability items under epic `f1fa`'s
  S5 tasks.
- **Gate substrate unchanged.** FFO (ADR 0040) remains the R5 backstop; the strict `Verified`
  `copyCondition` (drops on rebase) and the ADR 0041 `LLM-Review` `TRIVIAL_REBASE` carry are
  preserved exactly — the lander sits *on* that substrate and never edits it.
