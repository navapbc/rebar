# Phase 2 — Verification (work product: scored survivors + a persisted audit record)

> Read this at the start of Phase 2. Input = the Discovery finding pool. A **separate, independent**
> blue-team verifier re-grounds each finding as an **unproven claim to test** — it never assumes the
> finding is correct, and it never saw a severity/confidence to anchor on. It runs one aggregate pass
> over all findings and, per finding, emits coarse **impact attributes** + typed **binary
> sub-answers**. The orchestrator then scores each finding with pure arithmetic and applies the floors.

## Verifier discipline (put these in the verifier prompt)

- **Reason first.** Reason through each finding's sub-questions against the code before committing answers.
- **Charitable reading IS your skepticism.** Give the code its most reasonable reading; confirm the
  finding only if the problem still holds under it. If a reasonable reading dissolves it, answer
  `evidence_entails_finding = no`.
- **Absence / "missing X" findings get a higher bar.** Confirm X is genuinely absent from the
  *complete* artifact (whole module + call sites), not just the slice the finder saw.
- **Adopted-library contract (anti-FP).** If the "gap" is the documented contract of a maintained
  dependency the code commits to, that contract is the mitigation — don't require re-validating it.

## The 4 binary sub-questions (validity) — each `yes | no | insufficient | na`

- `is_verifiable` — the finding is concrete enough to check against the code (a real citation/metric).
- `evidence_entails_finding` **(load-bearing)** — the cited evidence actually *entails* the problem
  under a charitable reading.
- `impact_follows_necessarily` — the asserted harm *necessarily* follows, not merely possibly and not
  contingent on a separate unlikely mistake.
- `no_viable_alternative_explanation` — no benign reading dissolves it (intentional pattern,
  framework-mandated, generated/vendored code). **This carries the "looks bad but is fine" filter.**

Answer `na` for a sub-question that genuinely doesn't apply (excluded from the score); do not `na` the
load-bearing `evidence_entails_finding`.

*(Dropped vs. rebar's plan-review set, by design: `path_reachable` — dead/vestigial code is a valid
finding for janitor, not a reason to dismiss; `no_existing_mitigation` — janitor is exactly when we
rethink whether a better mitigation exists, so an existing one must not auto-dismiss; and
`severity_claim_justified` — findings carry no asserted severity to check.)*

## The veto — `cited_reference_accurate` (`yes | no | insufficient | na`)

Answer only when the finding cites a specific code reference. **A `no` is a hard drop** regardless of
scores — it catches the hallucinated-citation smell janitor itself hunts. `na` when there's no
specific citation to check.

## The 5 impact attributes — anchored to their levels (calibrate per finding; don't default to middle/top)

- `prod_impact` (`none|low|medium|high`) — runtime/user-facing risk as shipped. none = docs/test-only;
  low = cosmetic/rare-path; medium = degraded behaviour or a real recoverable gap; high = data loss,
  security exposure, core flow broken.
- `debt_impact` (`none|low|medium|high`) — maintainability/comprehension/changeability harm (janitor's
  dominant axis; the optionality lens scores here). low = local untidiness; medium = a seam that costs
  real rework; high = an architectural decision expensive to unwind.
- `blast_radius` (`local|module|system`) — how far the issue reaches.
- `likelihood` (`low|medium|high`) — chance the harm actually bites, **informed by the temporal pass**
  (a hotspot = high; stable rarely-touched = low).
- `reversibility` (`easy|moderate|hard`) — cost to fix / change course later (doubles as an effort
  signal for Phase 3).

## Deterministic scoring (orchestrator, no model)

Ordinal maps: sub-answers `yes=1.0, insufficient=0.5, no=0.0`; `none=0, low=.33, medium=.67, high=1`;
`local=.33, module=.67, system=1`; `easy=.33, moderate=.67, hard=1`.

- **validity** = graded fraction of the 4 binary sub-answers over the answerable (non-`na`) set. ∈ [0,1].
- **impact** = mean of four terms: `max(prod, debt)`, `blast_radius`, `likelihood`, `reversibility`.
  ∈ [~0.25, 1] (floors at ~0.25 — blast/likelihood/reversibility have no "none" level).
- **severity label** (record only): `critical ≥ 0.75`, `major ≥ 0.5`, `minor ≥ 0.25`.

## The floors (global; not tuned after launch)

The decision, evaluated per finding (a live known-fine shield drops it before the floors; see the
registry section below):

```
DROP if  cited_reference_accurate == "no"     (veto — inaccurate citation)
     OR  matches_known_fine == "yes"          (live, non-stale registry shield — stale entries already excluded)
     OR  validity < 0.75                       (low-confidence — <~3-of-4 sub-answers affirm it)
     OR  impact  < 0.5                          (immaterial — below "major")
else SURVIVE
```

- **veto** — an inaccurate citation is fatal regardless of scores.
- **known-fine** — a maintainer explicitly blessed this pattern and the blessing is still valid.
- **low-confidence** — we won't spend change-risk on a finding we can't substantiate. (No verification
  produced → treated as below the floor → dropped; re-run if you suspect a batch omission.)
- **immaterial** — the change-risk wouldn't repay the small gain.

Rationale: janitor's output is committed *work*, not an advisory comment, so the confidence bar is
higher than a review gate's; and "material improvement, risk spent wisely" puts the impact floor at
the minor/major boundary.

### Dismissal category (closed enum — per-run legibility)

Tag every dropped finding with exactly one `dismissal_category` so the report's Dropped section reads
as a count table and scope problems surface. Assign it at drop time; it refines the mechanical gate:

| Category | When |
|---|---|
| `KNOWN_FINE` | matched a live registry entry (record the entry id) |
| `BAD_CITATION` | veto — citation inaccurate |
| `INTENTIONAL` | deliberate / framework-mandated design (a benign reading holds) |
| `GENERATED` | generated code |
| `VENDORED` | third-party / vendored code, not owned here |
| `MISREAD` | cited evidence doesn't entail the finding (finder error) |
| `TOO_VAGUE` | not concrete/verifiable enough to act on |
| `IMMATERIAL` | real but below the impact floor (cold / local / trivial) |
| `SUPERSEDED` | merged into another finding this run (dedup) |

`GENERATED` and `VENDORED` are **scope signals**: if they dominate the drops, the audit is scanning
code it shouldn't own — narrowing the scan scope is the right fix, not re-verifying them every run.
This is a **per-run signal only** — janitor does **not** track dismissal categories across runs.

## Known-fine registry (shielding + staleness)

The registry (`.rebar-janitor/known-fine.md`) records patterns a maintainer has **explicitly blessed as
acceptable**. `matches_known_fine` is a **separate drop gate** — it is NOT one of the four validity
sub-questions, so the `validity ≥ 0.75` floor is computed exactly as above and stays unchanged.

**Governing principle — a ledger of human decisions.** You may read entries, compute staleness,
auto-remove entries that no longer apply, and *propose* (re)confirmations. You must **never silently
write or update an entry's acceptance** — every write of `confirmed_on` / `content_fingerprint` /
`blessed_instances` / `hotspot_at_confirmation` is a human (re)confirmation (Phase 4 accretion / Phase
5 re-confirmation). The machine can only ever *remove* a shield (more thorough) or *propose* a change
— never extend a blessing to code a human didn't approve.

**Entry format** — one `###` section per entry in `.rebar-janitor/known-fine.md`, with fields:
`id`, `location` (path/symbol/scope — the prefilter key, fingerprint anchor, and path-gone GC key),
`pattern` (semantic description of the accepted issue — the `matches_known_fine` target), `rationale`
(why accepted), `confirmed_on`, `content_fingerprint` (normalized hash of the covered construct at
confirmation), `blessed_instances` (the specific instances comprising the pattern at confirmation),
`hotspot_at_confirmation` (bool).

**Per-finding shielding flow:**
1. **Prefilter** — select entries whose `location` overlaps the finding (path-glob).
2. **Staleness** — an entry is **stale this run** if ANY trigger fires (a stale entry shields nothing):
   - **T1** (deterministic): recompute the covered construct's content fingerprint; stale if it
     differs from `content_fingerprint` **and** the change is not a pure shrink (the new text is not a
     strict line-subsequence of the old with smaller size — pure removals don't fire).
   - **T2** (narrow cheap-model yes/no): *"does the current code contain an instance of `pattern`
     outside `blessed_instances`?"* `yes` → the pattern spread → stale. (Membership over a concrete
     list, not a re-count — reliable and cheap.)
   - **T3** (deterministic): `hotspot_at_confirmation == false` **and** the entry's location is in this
     run's temporal hotspot set (a `false→true` transition only; "already hot, still hot" doesn't fire).
3. **Auto-GC** — remove an entry whose `location` no longer resolves (path gone), or whose `pattern`
   is no longer present at all when a stale entry is re-examined (pattern gone). No attention.
4. **`matches_known_fine`** (`yes | no | na`) — for surviving (non-stale) candidate entries, the
   verifier judges whether this finding matches the entry's blessed `pattern`. `yes` → the finding is
   **dropped as known-fine** (recorded in the report with the entry id). A finding whose only candidate
   entry was stale gets **no shield** → normal scoring → silently re-dropped if still
   immaterial/low-confidence, or surfaces as a survivor if it has become real + material.

**Hand-off to close-out:** record the set of **stale entries whose findings re-verified as still-fine**
(dropped by the floors this run) — Phase 5 offers these for batched re-confirmation. Entries whose
findings surfaced as survivors need no action; they retire via pattern-gone GC once the fix lands.

## Audit record (persist this; the record, not the action)

Write `.rebar-janitor/report-<YYYY-MM-DD>.md`:

```
# Codebase Health Report — <repo> — <date>

## Summary
- Overall health: <1-2 sentences>
- Top risks: the highest-impact survivors
- Counts: survivors by impact band and by concern; dropped by reason (table)

## Survivors  (ordered by impact)
### [severity] finding  (concern)  — validity <v>, impact <i>
- Citations / evidence
- Why it matters
- (its Remediation Plan item, once Phase 3+ has run)

## Hotspots
Survivors intersected with the temporal hotspots (complex × frequently-changed).

## Trends  (only if git history available)
Churn, clone-ratio direction, refactor ratio, most-active files.

## Dropped — "looks bad but is fine" / not worth it
Count table by `dismissal_category` (this run only), then the itemized drops each tagged with its
category (KNOWN_FINE carries the entry id). If GENERATED + VENDORED dominate, add a one-line scope
note (e.g. "18/34 drops under vendor/ — consider narrowing scan scope"). Stating drops explicitly
prevents false-positive fatigue and stops the next run re-flagging them.

## Tooling used this run
Exact commands/patterns run, with brief notes on what each surfaced.
```

Restate the summary and top survivors in your own message text (the file alone isn't a completion signal).

**Gate to Phase 3:** the survivor set (each carrying validity, impact, citations, why_it_matters).
Then read `phases/remediation.md`. If there are no survivors, say so and stop — there is nothing to
remediate this run.
