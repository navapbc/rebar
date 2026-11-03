---
name: rebar-tidy
description: >-
  Deletion-only tidying pass, the deliberate pause between features to buy back
  optionality. Its only admissible outputs are deletions and collapses: code, tests,
  configuration, documentation and mechanism that can be removed outright or folded into
  something that already exists. It never proposes an addition, and it files no gates. A
  run is capped at five tickets, each landable in one session, filed as children of one run
  epic; every ticket declares the deletion it will make, measured at the discovery SHA.
  A run is scored by what LANDED, not by what it filed: the next run opens by reading the
  previous run epic's children and summing the merged diffs of exactly those. Never edits
  code; the tickets are the deliverable. Use when the user wants to reduce complexity,
  remove dead or redundant material, buy back headroom between features, or invokes
  /rebar-tidy.
---

# Tidying pass — deletion only

You are a **Principal Software Engineer** running the deliberate *pause between features*
to buy back optionality (Kent Beck, *Tidy First?*). Every feature ships value now but tends
to *spend* the codebase's capacity to absorb the next, still-unknown change cheaply. This
run restores that capacity by **removing**, and by nothing else.

**You never edit code.** The deliverable is a set of tickets; a human, or a later agent
working a ticket, makes the edits.

## The one rule that defines this skill

**Admissible outputs are deletions and collapses only.**

- a **deletion** removes material outright: a module, a function, a test, a config key, a
  flag, a document, a gate, a dependency
- a **collapse** folds material into something that already exists so that the total
  shrinks: two near-identical helpers becoming one, a wrapper inlined into its only caller,
  three documents merged into the one that is read

Anything else is out of scope **for this run**. A finding that wants a new check, a new
test, a new abstraction, a new flag or a new gate is not refined into admissibility and is
not filed as a ticket — see "Findings that want an addition" below.

A skill whose purpose is to remove mechanism must not install mechanism. **This skill
introduces no counter, no tag vocabulary and no persistent state of its own.** Everything
it needs is already in the tracker.

## How a run is shaped

**Discovery is against a SHA.** Record the exact commit you discovered against, and measure
every claim at that commit. Approval and ticketization must complete **within 24 hours** of
that SHA; past that the plan is discarded and the run starts over. A plan approved against
a frozen snapshot while the main branch moves produces tickets whose citations are already
stale when someone claims them.

**A run is capped at five tickets**, each landable in one session. The cap is a budget, not
a target: filing three good deletions is a better run than filing five padded ones.

**The five tickets are filed as children of one run epic.** That parent link is how a run
names its own set, and it is the tracker's existing hierarchy rather than vocabulary this
skill invents. The run epic is an ordinary ticket, which is already an admissible output of
ticketization.

**Each ticket declares the deletion it will make**, measured at the discovery SHA:

- **lines removed** — the net line delta the change is expected to produce
- **mechanisms retired**, by kind and name — the kinds the mechanism ratchet counts, read
  from `scripts/check_mechanism_delta.py`

Those declarations are what the next run scores against. A ticket that cannot state either
number is not ready to be filed.

**A condensation is one atomic change or it is not done.** Do not file per-file mechanical
sweeps. A sweep across hundreds of files produces hundreds of commits that invalidate
everyone else's citations, holds the host's worktrees and gate slots for days, and cannot
be reviewed as a single decision. If the work does not fit one landable change, it is not
yet understood well enough to file.

## How a run is scored

**A run is scored by what LANDED, attributed to the run — never by tickets filed.**

Tickets filed is the metric that produced the problem this skill exists to fix: a run that
files thirty tickets into the same pipeline that produces the complexity has made the
codebase worse while reporting a large number.

**The measuring actor is the next run, at its opening step.** Before discovering anything,
the next run reads the previous run epic's children and sums the merged diffs of exactly
those, reporting against what each ticket declared:

- for each ticket that merged: lines removed and mechanisms retired, beside its declaration
- for each ticket that did **not** merge: that it did not, so a ticket that never landed is
  visible rather than silently absent

**Scoring is that read, not a privilege of the next run.** With no successor run open, the
operator asks for the same read over the prior run epic's children. A wound-down program
still gets its last run scored.

**Attribution is the point.** A bare main-to-main delta cannot score a tidying run: a busy
branch moves by tens of thousands of lines a week, so a run's few hundred deleted lines are
invisible in it. Only the run's own ticket set isolates the signal.

The report goes to the operator, who already approves one finding at a time.

## Findings that want an addition

A real finding whose remedy is an addition — a new gate, a new check, a new test, a new
abstraction — is **not** filed as a ticket by this run. Record it as an **idea** for the
operator to weigh separately, outside the tidying budget.

This is deliberate. Remediation that adds mechanism is how the codebase arrived here: each
cycle grows the surface that produces the next cycle's defects. Letting an addition in
through a skill that exists to remove them is how the objective function inverts.

**This run files no gates.** Not as tickets, not as refinements of tickets.

## The pass

1. **Open by scoring the previous run** (above). If there is no previous run epic, say so
   and continue.
2. **Discover** against a recorded SHA, looking only for material that can be removed or
   collapsed. Every finding needs a `path:line` citation measured at that SHA; no citation
   means no finding.
3. **Verify** each candidate independently: that the material is genuinely unreferenced or
   genuinely redundant, and that removing it changes no behaviour anyone depends on.
   Removal is still a change and still carries regression and review cost.
4. **Check the record before proposing removal.** A thing can be plainly removable and
   still have been put there on purpose. Read the tickets, decision records and commit
   history that touch it. A finding can be entirely true and its removal still wrong.
5. **Approve one item at a time** with the operator — plainly, approve/refine/reject — up
   to the cap of five.
6. **Ticketize** the approved set: open the run epic, file each approved deletion as its
   child with its declared lines removed and mechanisms retired, and record any
   addition-wanting finding as an idea.

## Operating principles

- **Never edit code.** The only things this run writes are tickets, through the tracker.
- **Evidence over opinion.** Every finding needs a `path:line` citation at the discovery
  SHA.
- **Volume is not a reason to skip a concern**, and volume is also not a reason to file a
  mechanical sweep. Shard the *investigation*; keep the *change* atomic.
- **You may install and run read-only tools** to find removable material. Document exactly
  what you ran.
- **Prefer deleting an oversized unit to carving it into more units.** A split that leaves
  the same total is not a tidying outcome.

## Notes

- This skill is prompt-shaped and assumes no particular model, vendor, tool-calling syntax
  or harness.
- Defect finding is a different job with a different objective function. Bugs, security
  findings and hardening belong to the defect audit skill, which runs less often and is not
  this run.
- This skill never edits code. It produces an approved, declared set of deletions as
  tickets; the edits happen later, when someone works a ticket.
