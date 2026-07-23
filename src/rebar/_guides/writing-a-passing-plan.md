# Writing a plan that passes the plan-review gate

**Audience: the plan author (you, writing a ticket description before you `claim` it).**
This is the on-ramp. It is hand-written and stays short. The authoritative, per-criterion
detail lives in the generated registry — `rebar explain <id>` or
[plan-review-criteria-guide.md](https://github.com/navapbc/rebar/blob/main/docs/plan-review-criteria-guide.md)
— and the gate's mechanics are in
[plan-review-gate.md](https://github.com/navapbc/rebar/blob/main/docs/plan-review-gate.md). Read
those *after* this when you need depth. (For the code-review sibling, run `rebar explain
review`.)

## The loop

The gate runs when a ticket **enters `in_progress`** (via `claim`, `transition`, or resume).
`rebar review-plan <id>` runs the full review and, on a non-blocking PASS, **signs an
attestation**; `claim` then does a fast, offline check that a fresh attestation exists. It
**coaches, not roadblocks** — only **blocking** findings stop you; advisories are suggestions.

```
draft description ──▶ rebar review-plan <id> ──▶ PASS (exit 0)? ──▶ claim <id>
                            ▲                     │
                            └──── revise ◀── findings (blocking = must fix)
```

Draft the description at `create` time, iterate while the ticket is `open`, and run
`review-plan` until it passes — *then* claim. **Only `review-plan`'s signed attestation gates
the claim.** `rebar clarity-check <id>` and `rebar check-ac <id>` are separate **self-help
scorers** (each exits 0/1) — useful to run, but they do **not** independently block the claim.

`review-plan` needs the `[agents]` extra + a model API key to run its LLM passes; without them
it degrades to the deterministic floor (fewer advisories, the hard structural checks still
run). The claim gate itself **fails closed**: no valid attestation ⇒ the claim is blocked;
`--force="<reason>"` is the audited escape hatch, not the normal path.

**Review in dependency order — dependencies before their dependents.** A review pins not just
the ticket's own material but its **direct dependencies'** (children, `depends_on`/`blocks`
prerequisites). If a dependency's plan changes after the review, the recorded PASS is
**invalidated** and a fresh `review-plan` is required — `rebar sign-review` will *not* certify
across a changed dependency. So review (and settle) a ticket's prerequisites and children
**before** the ticket itself, and **don't review a ticket and its dependencies in parallel**:
a child moving mid-review invalidates the epic's review and wastes the LLM run. Prefer
`next-batch` (dependency-ready, conflict-aware) over ad-hoc parallel review of related tickets.

## Leaf vs container decides which sections you need

Scrutiny is keyed on **whether the ticket has children**, *never* on its type. A ticket with
≥1 child is a **container**; a childless ticket of any type is a **leaf** (a childless epic is
a leaf; a story with children is a container). `bug` and `session_log` tickets are exempt from
several criteria.

- **Leaf** — does the work itself: needs `## Approach`, `## Scope`, `## Testing` (when it adds
  testable behavior), and `## Acceptance Criteria`.
- **Container** — delegates to children: needs `## Success Criteria` and a child decomposition
  where every deliverable is covered by a child and children don't overlap (G3/G4/G5). It
  **defers testing to its children** — don't write leaf-level tests on a container.

## The description template

Write these as Markdown headings. An **`## Acceptance Criteria`** block is always required
(the `check-ac` scorer looks for it). Apply the leaf/container rule above for the rest; a
purely mechanical change (refactor/rename/config/dep-bump/docs) legitimately has no `##
Testing` (the testing criterion only fires on *new testable behavior*).

- **`## Context / Problem`** — who has the problem and why now (F4). One or two sentences.
- **`## Approach`** — how you'll solve it; name the alternative you rejected and why (G6).
- **`## Scope`** — the files/modules you expect to touch (the edit-set, G1G2). See below.
- **`## Testing`** — for a leaf adding behavior: the happy path **and** a failure/edge/empty
  path; for a **cutover/migration**, a live end-to-end check (E5, T-overlays).
- **`## Acceptance Criteria`** — a `- [ ]` checklist of measurable, in-session-verifiable
  outcomes, each stating **how it's checked** (F1, E1, E6). **Required.**
- **Containers** additionally: **`## Success Criteria`** + the child decomposition.

## The blocking checklist — your plan MUST…

These are the criteria that actually block a claim. Fix every one before you claim.

- **Be executable without a clarifying question (`E2`).** No `TBD`, `figure out`,
  `verify whether`, `check if`, or `choose an appropriate X` in scope bullets — a placeholder
  is a design choice punted to the executor. State a **default** ("assume X unless told
  otherwise") instead of asking a question.
- **Be measurable and finishable this session (`F1`).** Each acceptance criterion names an
  observable outcome and how it's verified — not "works correctly".
- **Be internally consistent (`COH`).** No section contradicts another (testing vs.
  decomposition, sequencing vs. declared dependencies, approach vs. a stated constraint).
- **Have a grounded edit-set (`G1G2`).** List real paths. **Creating a new file is fine** —
  a to-be-created path is recognized as new work, not a hallucinated target; only *naming an
  existing symbol/file that doesn't exist* is flagged. Record paths with
  `rebar set-file-impact <id> '[{"path":"…","reason":"…"}]'` (the file-impact-coverage check
  nudges a leaf that declares none).
- **Ground its assumptions and asserted capabilities (`E4`, `asserted-capability`).** If the
  plan relies on something existing (a function, config key, library behavior), **cite the
  concrete evidence** — a `path:line` or a module/symbol name you actually confirmed by
  reading the tree (not from memory). Uncited assertions fail closed.
- **Choose a sound approach (`G6`).** No golden-hammer / cargo-cult / resume-driven /
  premature-optimization; name why the chosen approach beats the obvious alternative.
- **Decompose sensibly if it's a container (`G5`).** Children cover the work, are the right
  size, and are ordered by their real dependencies.

A finding blocks only when it lands on a blocking-eligible criterion above; advisory criteria
never block on their own (they're scored and coached, capped at the top ~20 per review).

## Advisories worth heeding (won't block, but strengthen the plan)

- **Right-size it (`A1`).** No new abstraction/dependency/config that YAGNI or Rule-of-Three
  (<3 call-sites) doesn't justify; don't rebuild what already exists.
- **Test the real path (`E5` + `T*` overlays).** Happy-path-only on a new user-facing flow, or
  offline/mock-only coverage of a path that defaults to a **live** boundary, both draw
  findings. Add the failure path; add a live end-to-end criterion for a cutover.
- **State value & intent fidelity (`F4`, `E3`, `ISF`).** Tie the work to the user problem and
  to the linked design/epic intent; don't drift from what the parent asked for.
- **Justify removals (`removal-rationale`).** Deleting something? Say why it's safe to remove.
- **Don't hedge (`hedge`).** "Should probably" / "might want to" in a requirement reads as an
  undecided choice — commit or drop it.

## Citing a prerequisite's symbol (`[rebar:<id>]`)

If your plan relies on a file, module, class, function, or config key that a **prerequisite
ticket** will create — something that does not yet exist on `origin/main` — the grounding
finders (`G1G2`/`E4`/`E6`) will otherwise flag it as missing. Cite it inline as
`<subject> [rebar:<id>]`, where `<subject>` names the relied-upon element and the trailing
`[rebar:<id>]` token names the prerequisite ticket (its id or alias). For example:

```markdown
- [ ] register the new adapter via `plugin_registry.register()` [rebar:1a2b-3c4d]
```

For the citation to be honored, there must be a **verified upstream edge** between the tickets:
either this ticket declares `depends_on -> <id>`, **or** the cited ticket declares
`blocks -> <this ticket>`. A `blocks` edge pointing *from* this ticket is downstream and does
**not** count. The edge must be **direct**: a *transitive*/indirect dependency — reaching the
prerequisite only through another ticket — does **not** satisfy it, so declare `depends_on` on
this ticket itself (or have the prerequisite `blocks` it) directly. When the edge is verified, the finder retrieves the cited ticket via
`show_ticket` and credits the symbol **only** if that ticket's plan/file_impact actually
establishes the specific functionality — an uncited, edge-unbacked, or coverage-unconfirmed
citation still grounds as normal (fails closed). So: add the `depends_on` edge (or ensure the
prerequisite `blocks` this ticket), and make sure the prerequisite's plan really delivers what
you cite.

## A minimum-viable passing plan (leaf task)

```markdown
## Context / Problem
`rebar export` writes UTF-8 but crashes on tickets whose title has a lone surrogate,
so a nightly export of the store aborts. Fixes the crash for the ops team.

## Approach
Encode with `errors="replace"` at the single write site in `exporter.write_line`.
Rejected: sanitizing on ingest — too broad, and it would rewrite historical events.

## Scope
- src/rebar/export/exporter.py  (the write site)
- tests/unit/test_exporter.py   (new case)

## Testing
- Happy path: a normal title round-trips unchanged.
- Edge: a title containing a lone surrogate exports without raising and is replaced.

## Acceptance Criteria
- [ ] Exporting a ticket with a lone-surrogate title no longer raises;
      verified by `pytest tests/unit/test_exporter.py::test_surrogate_title`.
- [ ] A normal title's exported bytes are unchanged; verified by the existing
      round-trip test still passing.
```

Note: paths are real, the rejected alternative is named (G6), each criterion says *how* it's
checked (F1), and there are no `TBD`s (E2). That's what clears the blocking bar.

## When you're blocked

`rebar review-plan <id>` exits **0 = PASS**, **1 = BLOCK**, **2 = INDETERMINATE** (something
couldn't be judged). Each finding names the **criterion id** (e.g. `E2`, `G1G2`) and the
evidence it cites; blocking findings are always shown, advisories are coached. The coaching
lines deep-link to the criterion's section (anchor `#<id>`), and `rebar explain <id>` prints
it — so a finding maps straight to the section of your description to fix. A blocking finding
is a revision request, not a verdict: revise that section, re-run `review-plan` to earn a
fresh signature, then claim.

Editing the description (its text, AC, file-impact, or decomposition) **stales an existing
attestation**, as does a new code commit or reopening the ticket — so re-run `review-plan`
after a material edit. Non-material changes (tags, comments, links, assignee) do **not** stale
it.
