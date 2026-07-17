# ADR 0043 — Operator-attested completion evidence

- **Status:** Accepted (story `jade-hay-hinge` / 7226)
- **Date:** 2026-07-10

## Context

The completion verifier (`src/rebar/llm/reviewers/completion_verifier.md`) certifies that a
ticket's acceptance criteria are demonstrably met before a work ticket closes, and — on a
certifiable PASS — its verdict is HMAC-signed onto the ticket. By deliberate design it is
scoped to what rebar can see without leaving its own process: the **codebase** (an attested
working-tree snapshot) and the **ticket system** (`show_ticket`). It has no access to
Gerrit, GitHub, AWS, CI, or any live system, and it treats ticket text as untrusted.

That scoping is correct, but it created a gap. Some completion criteria have "done" evidence
that inherently lives **outside the codebase**: a deploy happened, a live end-to-end run
passed, a console setting was flipped, an operator ran a drill. There is no code for the
verifier to read, so it could never mark such a criterion met — it could only FAIL, forcing
the operator to `--force-close` (which lands the ticket **unsigned**). This is exactly what
happened to the two-vote-gate epic 1fa8 (stories S3–S6 and the epic): the work was done and
proven live — recorded in `infra/runbooks/two-vote-gate-rollback.md` §C.1 — yet none of it
could earn a signature.

We want operational work to be able to close **signed**, without giving the verifier new
reach (no external tools, no separate evidence collector — those are out of scope for rebar
as a tool) and without opening a loophole that lets incomplete work self-certify.

## Decision

**Where DOES the "done" evidence live?** — Two, and only two, kinds of completion criterion:

1. **codebase-verifiable** (the default, unchanged) — the evidence is in the repo (a
   file/symbol/behavior). Verified against code exactly as before; the author's checkbox is
   never trusted.
2. **operator-attested** — the evidence inherently lives outside the codebase. The
   **admissible evidence is a concrete attestation recorded in the ticket system** (a comment
   / recorded artifact the verifier can read via `show_ticket`).

An author marks a criterion operator-attested with an inline tag at the start of the
checkbox text: `- [ ] [operator-attested] …`. Matching is **exact and case-insensitive** on
the token `operator-attested`; anything else — untagged, an explicit `[codebase]`, or a
malformed near-miss like `[operator_attested]` — is treated as **codebase-verifiable**. So
every pre-existing ticket is unaffected, and a missing/garbled tag fails safe to the
stricter bar rather than silently weakening it.

### What a signature means (bounded)

A signed completion verdict asserts: **"as far as the ticket and the codebase are concerned,
the stated criteria are met."** For a codebase-verifiable criterion that is a check against
real code. For an operator-attested criterion it is a check that the ticket carries a
**concrete** attestation of the outside-world fact. It is **not** a claim that rebar
re-verified the live system — that is outside rebar's reach and always was. The guarantee is
bounded and honest, not absolute.

### The concrete-vs-vague discriminator

An operator-attested criterion is **MET** only if an attestation names **≥1 verifiable
specific** — a reference ID/URL (change/PR/commit/deploy id), a named actor, a
measured/observed outcome (vote result, log line, console/metric value), or a
timestamp/date — AND those specifics substantively match what the criterion requires. It is
**NOT MET** if the attestation is absent, or merely asserts completion ("done", "works now",
"verified") with no such specific.

Canonical gray-zone examples:

1. *"Applied terraform to account 896586841071 on 2026-07-10; `terraform apply` reported
   `3 added`; the three CloudWatch alarms now show in the console."* → **MET** (names actor
   scope, a timestamp, a measured outcome).
2. *"Deployed and verified it works in prod."* → **NOT MET** (asserts completion, zero
   verifiable specifics).
3. *"Re-ran fetch-secrets; `.env` now carries `REVIEWBOT_TICKETS_PAT`; restarted the bot;
   `/review/health` returned 200; forced re-review of change 492 emitted artifact ticket
   8e68."* → **MET** (multiple named specifics that match the criterion). A borderline
   variant that says only *"secrets are set up now"* → **NOT MET**.

## Threat model — haste, not deception (and the injection split)

The threat this guards against is **hasty or incomplete** work, not a **deliberately
deceptive** author. A hasty author leaves an operational criterion blank or writes "done"; a
finished one records the specifics. The discriminator catches the former without any external
verification. We explicitly do **not** defend against an author who fabricates a plausible,
specific-looking attestation — that is deception, and rebar's signature has never claimed to
detect it (a human reviewer and the two-vote code gate are the defenses there).

This interacts with the verifier's **prompt-injection guard**, which must be preserved.
Ticket text is untrusted, and the guard forbids it from **commanding** a verdict ("you must
PASS", "ignore your rules"). Admitting an attestation as *evidence* looks superficially
similar to obeying an instruction, so the two are split by an explicit rule:

- a **command** tries to control the verdict → always ignored, for every criterion kind;
- an **attestation** reports a **checkable fact** (per the discriminator) → admissible
  *evidence* for an operator-attested criterion, which the verifier judges for substance
  rather than obeys.

This is the well-known **indirect prompt-injection** problem (untrusted content in the model's
context steering its output; cf. OWASP LLM01). Our position is narrow and safe *because* the
threat model is haste-not-deception: the verifier never lets ticket text *command* a verdict,
and a codebase-verifiable criterion is **never** satisfied by a comment alone — so the only
thing an attestation can do is supply a specific, checkable claim for a criterion the author
already tagged as living outside the codebase. A fabricated attestation is out of scope by the
same reasoning that a lying `- [x]` checkbox always was.

## Alternatives considered

- **A machine-readable criterion-kind field** (structured per-criterion metadata) instead of
  an inline prompt tag. Rejected: the admissible evidence is unavoidably natural language (a
  human-written attestation) that only the LLM can judge for substance, so a structured flag
  removes no judgment — it only selects which bar to apply, which a one-token tag already
  does at far lower cost.
- **A separate evidence collector / external tool access** (let the verifier call Gerrit /
  AWS / CI). Rejected: out of scope for rebar as a tool, expands the trust and secret surface,
  and is unnecessary given the bounded guarantee above.

## Consequences

- Operational work can close **signed** when it records concrete attestations; blank or
  hand-wavy ones still FAIL — and the finding now carries per-finding `remediation` telling
  the author to record proof (reference id, observed outcome, when).
- Existing tickets are unaffected (untagged → codebase-verifiable).
- The change is confined to the verifier prompt, the `VerdictFinding.remediation` field, the
  eval spec fixtures/rubric, this ADR, and the `docs/plan-review-criteria-guide.md` authoring note. `common.schema.json`
  and other reviewers are untouched.
