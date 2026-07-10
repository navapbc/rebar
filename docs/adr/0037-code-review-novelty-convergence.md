# ADR 0037: Code-review novelty convergence (per-citation region-gated rising floor)

- **Status:** Accepted
- **Context:** Epic *Cross-patchset code-review finding memory* (`374d-9aaf-7d5b-4331`,
  alias super-path-bag); children c639 (reader + payload), 3bff (local session
  persistence), e51b (region_gated_floor), c3e4 (plan-review surfaced-only fix), this
  doc child (de8a). Mirrors and diverges from ADR 0008 (plan-review convergent re-review /
  rising floor); relies on ADR 0002 (code-drift invalidation) for the content-addressed
  resilience argument.

## Problem

Code review re-reviews a Gerrit patchset (or a local `rebar review-code` run) FRESH, with
no memory across runs. Plan review already converges via a novelty rising floor (ADR 0008):
on re-review it loads the prior run's SURFACED findings, an LLM novelty sub-call scores each
current finding against the priors, and a deterministic predicate drops NOVEL + low-priority
findings so the remediation loop converges instead of surfacing fresh trivia each pass. Code
review had no comparable mechanism, so an author iterating a change was re-nagged with the
same low-impact nits every patchset.

## Decision

Bring plan-review's novelty machinery to code review, REUSING the shared kernel primitives
unchanged and adding only the gate-specific orchestration — with one deliberate divergence:
because a code finding carries a SOURCE CITATION, the floor is gated PER-FINDING on whether
the cited code REGION changed since the last review.

### Reuse boundary (shared kernel vs code-review-specific)

- **Reused UNCHANGED** (no fork): the novelty SCORING CONTRACT
  (`review_kernel.verify.novelty_model` / `NOVELTY_SUBANSWERS`), the reshaper
  `review_kernel.verify.reshape_novelties`, the per-finding novelty math
  `review_kernel.decide.novelty`, and the drop predicate
  `review_kernel.decide.rising_floor_drop(priority, novelty, *, t_novel, floor)`. Code review
  binds the SAME `novelty_model` under the `code_review_novelty` output-schema name.
- **New, code-review-specific** (the prompt/orchestration is genuinely gate-specific, so no
  shared abstraction spans it): the `code_review_novelty.md` reviewer prompt (diff domain
  context; prior findings appended to the instructions; the `code-novelty` reviewer id); the
  `region_gate.py` region vocabulary + detector; `workflow_ops.apply_region_gated_floor`; and
  a thin `score_code_novelty` that mirrors plan-review's `_score_floor_novelty` runner wiring
  but ALSO surfaces `matched_prior_id` (which the kernel `score_novelty` wrapper discards)
  for carryover labeling.

### The per-citation region gate (the divergence from plan-review's whole-artifact floor)

`region_gate.py` defines a closed tri-state vocabulary — `REGION_UNCHANGED`, `REGION_CHANGED`,
`REGION_UNKNOWN` — as constants (not prose). The detector compares each cited file's CURRENT
sha256 to the prior review's `deps` map: equal → UNCHANGED; differ → CHANGED; a path absent
from the prior deps, a multi-location / absence-evidence finding, a moved/renamed file, a
create/delete (`absent` sentinel), or ANY error → UNKNOWN.

`apply_region_gated_floor` drops an advisory finding iff `rising_floor_drop(...)` is true
(NOVEL and low-priority) **AND** its region is `REGION_UNCHANGED`. `REGION_CHANGED` and
`REGION_UNKNOWN` ALWAYS raise. This is the fail-safe direction — a broken/ambiguous signal can
only make the gate STRICTER (surface more), never drop wrongly. The whole floor is additionally
wrapped in try/except so any reader/hash/novelty error leaves the verdict fully unfiltered.

### Content-addressed region detection (resilience)

Region state is content-addressed: the artifact stores a per-reviewed-file content-hash map
`{path: sha256}` (reusing the private `plan_review.attest._hash_file` primitive — NOT the
plan/ticket-coupled `dependency_hashes`). Next run re-hashes the current files and compares.
Because it keys on CONTENT, not a reachable commit, it survives a rebase / force-push (which a
reachability-based scheme would not). File-level in v1; line-level (reviewdog-style, via
`git diff --no-index` against a stored snapshot) is a deferred follow-on.

### Surfaced-only prior set

Novelty is scored ONLY against findings RETURNED TO THE CLIENT — for code review the union of
the persisted `blocking` + `advisory` buckets, never `coaching` or any dropped bucket. This is
the code-review counterpart of the plan-review fix (child c3e4 / bug old-frilly-plankton):
feeding previously-dropped findings back into the novelty prior set would let a finding
permanently floored for convergence re-match on recurrence, score low-novelty "carryover", and
escape the floor that dropped it — defeating the permanent drop.

### Permanent low-impact drop is the intended compromise; the priority floor is the escape hatch

Dropping a NOVEL low-impact finding is PERMANENT, defined precisely: the drop persists across
re-reviews FOR THE SAME KEY (session or change). It does NOT reset per review the way plan-review's
floor re-scores each run — because the surfaced-only reader excludes a dropped finding from the
prior set, so on recurrence it scores as NOVEL and is dropped AGAIN (as long as it stays
low-priority + region-unchanged). Permanence is WITHIN one keyspace, never cross-key. This is the
convergence compromise, accepted deliberately. The escape hatch is the PRIORITY axis: only findings
below `novelty_priority_floor` are eligible, so a higher-impact finding keeps surfacing. We TRUST
the priority score — there is no categorical criterion exemption; recalibration lives on the
priority axis, not the novelty axis.

### Activation (no new flag, no eval gate)

The floor reuses plan-review's EXISTING config keys — `verify.novelty_drop_threshold`,
`verify.novelty_priority_floor`, and `verify.novelty_drop_active` — introducing NO new
config key. `novelty_drop_active` is the shared evidence gate: OFF by default, so an operator
opts in with evidence exactly as plan-review requires. On top of the flag the floor SELF-GATES:
it is inert whenever there is no prior memory for the current key (first review, or a reader
error). Both gates must be open before anything drops.

### Typed keyspaces (local session vs Gerrit change)

Memory is addressed by a TYPED key, into two disjoint keyspaces:

- **Local** `rebar review-code` → `session:<session_id>` (artifact title
  `code-review: session:{id}`), keyed on the resolved session id (a per-invocation uuid4 when
  no session lifecycle exists, so a bare invocation is intentionally isolated / inert).
- **Gerrit** → `change:<change_id>` (artifact title `code-review: {change_id} @ {revision}`),
  keyed on the CHANGE and spanning its revisions/patchsets.

The keyspaces are disjoint by construction (the reader matches only its own title scheme), so a
prior LOCAL review can NEVER seed a change's FIRST Gerrit review — the property that keeps a
developer's local iterations from silently narrowing the authoritative Gerrit gate.

### Neutrality (ADR 0008 Invariant 1 preserved)

The Pass-1 finder never receives prior findings; only the post-find novelty seam does. Prior
findings enter ONLY `apply_region_gated_floor`'s novelty sub-call, which runs AFTER the
workflow produces the verdict — so the finder's neutrality is preserved by construction.

## Explicitly NOT built (and why)

We deliberately did NOT add any of the following heavier convergence machineries (defined here so
the rejection is evaluable):

- a **DSO-style arbiter** — a standalone decision/signal orchestrator that ADJUDICATES between
  competing findings/signals;
- a **relation-taxonomy** — a typed model of relationships BETWEEN findings
  (duplicates / supersedes / refines / …);
- an **oscillation-loop** — a detector that tracks a finding surfacing → dropping → resurfacing
  across runs to break cycles.

The lightweight novelty floor suffices: convergence is achieved by a permanent, priority-gated
drop of novel low-impact findings on unchanged regions, which monotonically narrows the surfaced
set across patchsets without modeling oscillation or inter-finding relationships. A heavier arbiter
would add machinery and failure modes for no demonstrated benefit over the floor. This positively
discharges the epic's original oscillation/convergence-signal criterion as a DESIGN decision, not a
deferral.

## Implementation status

Every element below is IMPLEMENTED on this epic's stack (hence status Accepted, mirroring ADR 0008's
record-of-implementation stance):

- Reader + payload (session_id + `{path: sha256}` deps map): `src/rebar/llm/code_review/sidecar.py`
  (`build_payload`, `reviewed_file_hashes`, `_cited_paths_code_review`, `latest_code_review_result`)
  — child c639.
- Local session persistence (typed `session:<id>` artifact, uuid fallback): `_cli/_llm_commands.py`,
  `code_review/shim.py`, `workflow/gate_dispatch.py` — child 3bff.
- Region vocabulary + detector: `src/rebar/llm/code_review/region_gate.py` — child e51b.
- Novelty prompt + reviewer id: `src/rebar/llm/reviewers/code_review_novelty.md` (`code-novelty`).
- `code_review_novelty` contract (= the kernel `novelty_model`): `code_review/contracts.py`.
- `score_code_novelty` + `apply_region_gated_floor`: `code_review/workflow_ops.py`; wired before the
  emit in `workflow/gate_dispatch.py`, keyed by the typed keyspace.
- Surfaced-only novelty prior set on the plan-review side: `plan_review/sidecar.py`
  (`surfaced_findings`) + `plan_review/__init__.py` — child c3e4 / bug old-frilly-plankton.

Follow-ons (DECIDED, not yet implemented): line-level region detection (currently file-level), and
threading the Gerrit `change_id` from the review-bot into the gate so the `change:<id>` keyspace is
exercised end-to-end (the machinery + key resolution ship here; the local `session:<id>` path is
fully wired).

## Consequences

- Code review converges across patchsets/sessions the way plan review converges across
  re-reviews, with the extra safety of the per-citation region gate (a changed region always
  re-raises).
- The mechanism is OFF by default; enabling it is an operator decision gated on evidence.
- Line-level region detection and full Gerrit-bot change-key wiring are follow-ons; the
  machinery and the local session path ship here.
