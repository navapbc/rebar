# Phase 4 — Approval (work product: an approved / refined item set)

> Read this at the start of Phase 4. Input = the ordered Remediation Plan. Present it to the user
> **one item at a time**.

## Presenting each item — plain, concise, positive; never a wall of text

For each item, write a short **situation → move → why it improves things**:

- *the situation* — one sentence on what's costing us (finding + why it matters), with the citation.
- *the move* — one sentence on what we'd do (the remediation), framed as the improvement it buys.
- *provenance* — "both proposers agreed" / "adopted from OSS: `<projects>`" / "no consensus — pick
  one:" then the alternatives.

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
