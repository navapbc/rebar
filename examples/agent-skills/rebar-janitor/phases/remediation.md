# Phase 3 — Remediation (work product: a Remediation Plan)

> Read this at the start of Phase 3. Input = the Phase-2 survivor set. Two remediation subagents run
> **in parallel, blind to each other**, over the survivors. Their independence is what makes agreement
> a real signal.

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

## Finding-completeness → OSS-research tiebreak

Convergence is judged per move, but coverage is tracked **per survivor** so nothing falls through: any
survivor **not covered by a converged move** sends its divergent candidate moves to a **research
subagent** — *how do popular, actively-maintained OSS projects handle / avoid / address this concern?*

- **Strong OSS convergence** (a clear dominant pattern across several popular, actively-maintained
  projects) → adopt it as the move, tagged **`oss-adopted`**, projects cited.
- **Weak/mixed OSS** → a **`no-consensus`** plan item carrying the candidate approaches as
  alternatives for the user to choose/refine in Phase 4 (never silently dropped).

## Remediation Plan

One item per move: `remediation` (approach + end_state), `finding_refs` (union), `provenance`
(`agent-converged` | `oss-adopted` | `no-consensus`), `impact` (max over covered survivors),
citations, `cascade_flag`, `effort`. Order by impact.

**Gate to Phase 4:** the ordered Remediation Plan. Then read `phases/approval.md`.
