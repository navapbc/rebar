# Phase 4 — Approval (work product: an approved / refined item set)

> Read this at the start of Phase 4. Input = the ordered Remediation Plan. Present it to the user
> **one item at a time**.

## Presenting each item — plain, concise, positive; never a wall of text

For each item, write a short **situation → move → why it improves things**:

- *the situation* — one sentence on what's costing us (finding + why it matters), with the citation.
- *the move* — one sentence on what we'd do (the remediation), framed as the improvement it buys.
- *provenance* — "both proposers agreed" / "resolved by research: `<record cited>`" / "no consensus —
  pick one:" then **both positions with their evidence and buckets**, never a synthesised winner.
- *prior decision* — **whenever Phase 2 emitted a `prior_decision`, surface it here**: the ticket /
  ADR / commit, what was decided, and — for a move that reverses it — what evidence has changed since.
  The user decides whether the past decision still holds; the pipeline's job is to make sure they are
  never asked to approve a reversal without knowing one is being proposed.

Also flag any item Phase 2 reclassified as **`HARDENING`** rather than `DEFECT` (no reachable harm
was demonstrated). These may still be worth doing — they must simply never be presented as bugs.

Use positive framing for the directive. Do not dump the raw finding/evidence blob — distill it.

## The verdict per item — approve / refine / reject

The user responds **approve / refine / reject**:

- **approve** — the item enters the approved set as-is.
- **refine** — capture the user's refinement verbatim; the refined item enters the approved set.
- **reject** — the item is dropped from ticketization; record the user's reason if given.

## Registry-add offer (known-fine accretion)

Offer to add a finding to the known-fine registry (`.rebar-janitor/known-fine.md`) in **either** case:

- **All remediations for a finding are rejected** — the maintainer won't fix it, i.e. it's acceptable
  as-is; or
- **An approved `defer → known-debt` move** — the maintainer accepted "leave it for now."

Ask whether to bless it. On **yes**, create an entry with the governing principle in mind (this write
*is* the human confirmation):

- `location` / `pattern` — from the finding (where + the semantic nature of the accepted issue).
- `content_fingerprint` / `blessed_instances` / `hotspot_at_confirmation` — computed from the current
  code and this run's temporal pass.
- `confirmed_on` — now.
- `rationale` — **auto-drafted** from the finding plus the user's rejection/defer reason(s); the user
  confirms or tweaks it in **one line** (never a blank prose box).

## Hand-off

**Gate to Phase 5:** the approved / refined item set, plus any newly-created registry entries. Then
read `phases/ticketization.md`. If nothing was approved, say so and stop.
