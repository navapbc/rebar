# ADR 0066 — Gate context is never elided; an oversized plan BLOCKS (P8), it is not summarized

- **Status:** Accepted (epic `b5bc`; ticket `ad16`)
- **Date:** 2026-08-08

## Context

The plan-review gate reviews a ticket's **whole** plan plus its direct children, each whole. The
context is assembled once (`plan_review/context_assembly.py` → `det_floor.PlanContext`, whose
docstring records "the content is ALWAYS whole (no truncation, no content-chunking, by design)")
and the orchestrator's flow states "content is ALWAYS whole; never truncated, never
content-chunked." When the LLM tier fans out, it facet-chunks the **rubric** and reviews
(parent + one child) pairings — each side whole — but it never chunks or shortens the plan
**content** itself.

This is a security invariant, not an ergonomic one. A plan-review PASS with no findings can be
**signed** into a durable attestation that the claim gate later reuses to let a ticket be worked
without re-review. If the reviewer were shown a **partial** plan — a summary, a truncation, a
window-fitted excerpt — it could miss a defect that the full text contains, reach a clean PASS,
and that false PASS would be **signed**. The elision would be invisible in the certificate. The
same reasoning is why `det_floor` refuses to reach a clean PASS on a plan whose **hierarchy
context is known-incomplete** (`hierarchy_incomplete`, ticket `b24d`): an incomplete read must
not certify.

## Decision

**Gate context is never elided.** The plan/ticket content fed to the review is always whole, and
the gate has exactly one response to content that will not fit the largest configured context
window even at one-criterion-per-call: the **P8 reviewability check BLOCKS** ("the ticket is too
large to review in full … reduce/decompose it" — the extreme of P4 / G5), whose finding states
the impact plainly: *"A plan that exceeds the largest context window cannot be reviewed whole; any
review would see a partial plan."* The author must reduce or decompose the ticket; the gate does
**not** shrink the plan to make it pass.

The single, bounded exception is **supporting** context, not the plan: the ISF criterion's
oversized linked **session log** may be summarized to fit (`pass1.py::_linked_session_log` →
`passes.summarize_for_isf`). That summarization is explicitly scoped — "the supporting context
only — **never the plan**" — is **recorded** on the coverage (`isf.summarized`), and the resulting
findings carry **reduced confidence** and an evidence tag. Summarizing supporting material with a
recorded confidence penalty is sound; summarizing the plan under review is not.

## Consequences

- The "content is ALWAYS whole / never truncated / never content-chunked / the plan is never
  summarized" comments in `orchestrator.py`, `det_floor.PlanContext`, `p8_reviewability`, and
  `pass1.py` are CURRENT safety invariants and stay in the source; this ADR is their fuller
  durable home and is cited from the assembly/P8 seam.
- Any future change that makes the reviewer read less than the whole plan (a summarizer, a
  truncator, a window-fitter applied to plan content) reopens this decision and must be argued
  here first — because it is the withdrawn-alternative attack vector below.

## Alternatives rejected

- **WITHDRAWN — summarize/truncate an oversized plan to fit the window instead of blocking P8.**
  This is a **signed-false-PASS attack vector** and is the whole point of this ADR: a reviewer
  shown a shortened plan can miss a defect the full text contains, return a clean PASS, and that
  PASS gets **signed** into a durable attestation the claim gate reuses — an unreviewed plan
  wearing a valid certificate, with the elision invisible in the manifest. An author (or an
  adversary) could even pad a plan past the window on purpose to trigger the summarizer and launder
  a defect past review. The only sound outcome for un-fitting **plan** content is therefore to
  BLOCK (P8) and require reduction, never to elide. This warning is retained deliberately; do not
  "optimize" the gate by summarizing the plan.
- **Certify on a known-incomplete hierarchy read.** Rejected for the same reason: an incomplete
  read cannot back a clean PASS, so `hierarchy_incomplete` forbids it.
