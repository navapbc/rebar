# Phase 3 — Remediation (work product: a Remediation Plan)

> Read this at the start of Phase 3. Input = the Phase-2 survivor set. Two remediation subagents run
> **in parallel, blind to each other**, over the survivors. Their independence is what makes agreement
> a real signal — but blindness alone only decorrelates *wording*. See the asymmetry below, which
> decorrelates *evidence*.

## The two proposers answer DIFFERENT questions

Both read the same code and the same survivors. What differs is the question and the evidence each is
given:

| | Question | Evidence access |
|---|---|---|
| **Proposer A — community** | *How do popular, actively-maintained OSS projects handle this?* | **web search**, plus the code |
| **Proposer B — project** | *How does this project already handle this?* | the **record** — tickets, ADRs, `git log`/`blame`, the Phase-2 `prior_decision` — plus the code |

Two blind agents drawing on *identical* evidence share their blind spots, so their agreement measures
prompt stability rather than correctness. Splitting the question is what makes agreement mean
something — and it makes **disagreement informative**, because each proposer can now know something
the other structurally cannot.

It also covers an error class neither would catch alone: claims about a *remediation property*
("renaming an unused parameter is safe") are answered by the community record, not by this codebase —
while claims about whether a change reverses a deliberate choice are answered only by the project
record.

## What each proposer returns

A set of **remediation moves**, each: `approach` (direction, not a patch), `targets` (artifact(s) to
change), `end_state` (the shape the code ends in), `finding_refs` (one or more survivor ids it
addresses — group freely), `effort_risk` (informed by `reversibility`), and an optional `cascade_flag`.
A proposer may also return `defer → known-debt` for a real survivor with no low-risk move.

## Proposer HARD-GATEs (do not weaken)

- **Independence** — the two proposers never see each other's output.
- **Scope-creep prohibition** — a move addresses its finding(s); no opportunistic expansion.
- **Cascade awareness (lightweight only).** A move that deletes/moves/renames a symbol sets a
  `cascade_flag` and adds a one-line *"consider caller + dynamic-reference (reflection / string
  dispatch / DI / dynamic import) impact"* note for the ticket. **Do not** reimplement a full caller
  sweep here — the rigorous check is the tracker's/implementer's job. janitor only raises the flag.
- **Propose as final** — propose the best move on its merits, not a placeholder to be fixed later.

## Convergence — matched at the move level, attribution-agnostic

We care whether both agents independently propose the **same move**, not how they attributed it. For
each cross-agent pair of moves, a small binary judge asks:

- `same_approach` (`yes|no|insufficient`) — substantively the same technique/direction?
- `same_end_state` (`yes|no|insufficient`) — the code ends in the same shape?

**Converged iff `same_approach == yes` OR `same_end_state == yes`.** (`same_target` is deliberately
*not* a criterion — same target alone is too weak.) A converged move enters the plan tagged
**`agent-converged`**, closing the **union** of both agents' `finding_refs` for it.

## Divergence → a "has this been explicitly rejected?" round

Convergence is judged per move, but coverage is tracked **per survivor** so nothing falls through.
Divergence is **not a tie to break** — Proposer A already *is* the OSS research, so there is no
neutral third party left to arbitrate, and picking a winner would discard the most informative thing
the phase produces. Divergence means one proposer knows something the other structurally cannot.

Any survivor **not covered by a converged move** triggers one targeted round asking **why**, put
symmetrically to both records:

- *Has the community explicitly rejected the approach this project uses?*
- *Has this project explicitly rejected the approach the community uses?*

Sort each answer into a bucket — they carry very different weight:

| Bucket | Evidence | Weight |
|---|---|---|
| **Explicitly rejected** — adopted, then reverted, with stated reasons | strongest against that approach | high |
| **Excluded at adoption** — the cost was predicted, never paid | informative about *anticipated* cost only | medium |
| **Never considered** — absent from the record | silence is not rejection | low |
| **Adopted and kept** — live counter-examples, named | direct refutation of "nobody does this" | high |

The distinction is load-bearing, not bookkeeping. "38 of 43 projects don't do X" reads as rejection
until you ask whether anyone tried X and reverted — and if the answer is *nobody did, every exclusion
was added in the same commit that adopted the linter*, the evidence reclassifies from **rejected** to
**excluded at adoption** and gets substantially weaker. The follow-up round can move the conclusion in
either direction; that is why it is worth running rather than a formality.

**Neither record is authoritative.** They fail in opposite directions: the project record is an
**N of 1** — high context-fit, small sample, possibly stale — while the community record has
statistical weight but low context-fit and **may be cargo-culted**, since an "excluded at adoption"
population is by definition one that never tested its own prediction. Report which side has actually
been *tested*.

Outcome:

- **A bucket-`high` answer on one side** → adopt that side's move, tagged **`research-resolved`**,
  with the record cited.
- **Anything else** → a **`no-consensus`** plan item carrying **both positions, their evidence, and
  their buckets** for the user to decide in Phase 4 (never silently dropped, never synthesised into a
  winner).

## Remediation Plan

One item per move: `remediation` (approach + end_state), `finding_refs` (union), `provenance`
(`agent-converged` | `research-resolved` | `no-consensus`), `impact` (max over covered survivors),
citations, `cascade_flag`, `effort`, and — where Phase 2 emitted one — the `prior_decision` record
with its ticket/ADR/commit reference. Order by impact.

A move that reverses a recorded decision MUST carry `evidence_changed`: what has changed since, and
why the original reasoning no longer holds. Its absence is what Phase 2's validity penalty scores.

**Gate to Phase 4:** the ordered Remediation Plan. Then read `phases/approval.md`.
