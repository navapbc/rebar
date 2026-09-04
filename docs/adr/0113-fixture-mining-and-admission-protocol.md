# ADR 0113 — Fixture mining and admission protocol: what asserts a criterion's required behavior

**Status:** Accepted (epic `spin-cub-usher` / `65dd-e93b-666a-4caa`)
**Date:** 2026-09-04
**Relation:** EXTENDS ADR 0109 (the plan-review replay harness), which is NOT superseded.
ADR 0109 answers how a contributor validates a *prompt/pipeline change* by re-running the
pipeline against real material. This ADR answers a different question that 0109 left open:
once a criterion exists, what body of evidence *asserts its required behavior* as a durable
regression fixture, and under what bar is that evidence admitted. 0109 is the instrument;
this ADR is the admission protocol that decides which mined fixtures are trustworthy enough
to become a criterion's calibration set.

## Context

The fixture-mining subsystem (`src/rebar/llm/evals/fixture_*`, and the scheduled heal loop
in `src/rebar/llm/evals/fixture_mining/`) mines per-criterion regression fixtures from the
persisted `REVIEW_RESULT` sidecar corpus — the model's own recorded verdicts on real
tickets — rather than from hand-authored examples. That corpus is the calibration
instrument ADR 0054 established and ADR 0109 re-used.

The protocol carries several rules a future contributor cannot infer from the code: which
signals are admissible evidence and which are not, why an escaped-defect label is used
positively but never negatively, why a fixture must reproduce before it is admitted, why
mined fixtures are excluded from the weekly live sweep, and why they live in this
repository rather than shipping as packaged defaults. An admission bar that is not written
down is an admission bar that erodes: the first contributor to add a fixture by hand will
not reproduce rules that exist only as code. This ADR records the decisions so the bar
survives.

## Decision

### 1. Admissible evidence is the persisted review verdict, carried with its provenance

The only admissible evidence for a mined fixture is a finding the plan-review model already
recorded in the `REVIEW_RESULT` sidecar corpus, paired with the provenance of the review
that produced it — the originating `ticket_id` and `review_event_uuid`. A fixture is not
authored from an opinion about what the criterion *should* say; it is mined from what the
gate actually decided on real material. Every admitted case and every drift entry carries
that provenance so a reviewer can trace a fixture back to the review it came from.

### 2. The escaped-defect label is positive-only evidence

A ticket carrying an escaped-defect label — a defect that reached `main` and was later
found — is admissible **positive** evidence: the criterion that should have fired becomes a
must-fire fixture. The absence of that label is **never** admitted as negative evidence
that no defect existed: absence is not evidence. This positive-only rule keeps the mined
must-not-fire set honest — a pass case is admitted only from a review that recorded a pass,
never inferred from an unlabeled ticket.

### 3. The vintage gate drops evidence older than its rubric

A candidate is admissible only if the criterion's rubric has not changed since the review
that produced the candidate (a git-log vintage check against the base ref). Prompt churn
invalidates old model verdicts — a finding recorded under a prior rubric no longer asserts
the current criterion's behavior — so a stale-vintage candidate is dropped before it can
reach the reproduction stage. The vintage gate is what makes a corpus-mined fixture safe in
a codebase where rubrics are edited.

### 4. Tiered admission bars: a blocking fixture needs the full fire-signal set

A mined fire candidate is admitted at the **blocking** tier only when it carries all three
fire signals — `reproduction_consensus`, `author_response`, and `margin` (a decision margin
at or above the blocking-tier floor `MIN_MARGIN`); a candidate carrying at least one signal
but not the full set is admitted **advisory** (a mined pass candidate is always advisory).
The blocking admission bar is deliberately stricter because a blocking fixture that misfires
costs a false CI failure while an advisory one only costs a note. The tier is recorded on
every candidate row so the admission decision is auditable rather than left to per-run
judgment.

### 5. Reproduce before admit — reproduction consensus, then an epoch majority

A candidate is never admitted on a single recorded verdict; two distinct reproduction bars
apply. First, at **selection**, a fire candidate must carry the **reproduction consensus**
signal: at least two backing reviews that share the same `material_fingerprint` recorded the
finding (equal-fingerprint reviews are reproduction pairs). Second, at **admission**, each
rehydrated case is re-run over the configured number of epochs and its observed behavior is
the **majority** of those epochs — fire iff the case fired in at least `(epochs // 2) + 1` of
them (so with the default three epochs a case must fire twice, but the rule is the majority,
not a fixed count). A case whose observed majority matches its predicted `expect` is admitted;
one that disagrees is *drift*: it is withheld, recorded with its direction
(`predicted`/`observed`) and reason (`non-reproducing`, `unbalanced`, `invalid-spec`), and
never silently admitted. A criterion that loses one side of its fire/pass balance to drift is
unbalanced and emits no spec.

### 6. Change-triggered execution — mined fixtures stay off the weekly cron

Rubric regressions arrive with commits, not with time, so mined-fixture evaluation is
**change-triggered**: it runs on rubric-change commits (the `eval-changed-rubrics` CI job,
selecting criteria whose rubric changed since the base ref) and is deliberately excluded
from the weekly live sweep. This keeps provider-token spend proportional to actual rubric
churn instead of paying a fixed weekly cost for criteria nobody touched. The execution
surface is portable: the selection and dry-run preview run in-process with no CI provider
required, so the rule is not bound to any one CI system.

### 7. The gap-only heal loop and its `unreliable-criterion:` breaker

The scheduled heal loop is **gap-only**: it mines only criteria that the gate routes but
that still lack an eval spec. A criterion already covered by a spec is out of scope. A
criterion the loop cannot mine reliably is quarantined behind a breaker rather than retried
forever: a per-criterion failure counter reaching the threshold (default 3 — reproduction
failures and emitter `skipped-unbalanced` results accrue the same counter), or a single
runner declaration that
the criterion is un-minable (`container-material-unrecoverable` for container criteria,
`not-inline-admissible` for the inline-unadmissible class), file exactly one open ticket
titled `unreliable-criterion: <criterion-id>`. That ticket excludes the criterion from
future selection until a human resolves it, so a criterion that cannot be mined stops
burning budget instead of failing silently every run. Filing is idempotent — a re-run while
the ticket is open files no duplicate — and the loop advances its due-stamp only after the
configured interval, so it is safe to invoke on a schedule.

### 8. Mined fixtures live in this repository, not as packaged defaults

Mined fixtures are committed to this repository rather than shipped as packaged defaults so
that the admission bar and every fixture's provenance stay reviewable in the same tree that
reviews the rest of the gate. A packaged default would move the evidence out of review; an
in-repo fixture keeps it where a contributor already looks.

## Consequences

- A contributor adding a fixture by hand has a written bar to meet: mined from a recorded
  verdict, positively labeled, in-vintage, reproducing by consensus and epoch majority, at
  the tier its blast
  radius warrants.
- `docs/plan-review-gate.md` links this ADR from the document contributors already read, so
  the protocol is reachable from the gate's contributor guide.
- The heal loop's breaker means an un-minable criterion degrades to a filed ticket, not a
  silent recurring spend — the failure is visible and owned.

## References

- ADR 0109 — Plan-review replay harness (extended, not superseded).
- `docs/plan-review-gate.md` — the contributor-facing gate protocol that links this ADR.
