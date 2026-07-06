---
schema_version: 1
title: Behavioral-prohibition consumer scan
description: Plan-review overlay T13 (AGENT, LLM-routed). Gap-report G-5. The rubric
  the Pass-1 finder applies for a plan that newly FORBIDS a previously-permitted action;
  routing in criteria_routing.json. See ADR 0034.
execution_mode: agentic
category: plan-review-criterion
dimension: overlay-prohibition
---
OVERLAY — apply only when the plan NEWLY FORBIDS a previously-permitted action: it introduces an
enforcement/gate that will start rejecting something that used to be allowed. Trigger lexicon:
"block", "reject", "require … before", "enforce", "must pass", "cannot merge until", "deny",
"fail the build if". If the plan introduces no new prohibition, PASS as not-applicable.

ENUMERATE THE INVISIBLE AFFECTED SET. A new prohibition silently breaks existing call sites that
perform the now-outlawed behavior — nothing in the remaining plan references them, so they are
invisible unless enumerated. Translate the prohibition into concrete grep patterns over EXISTING
call sites of the behavior being outlawed, then Grep/Read to find them. Worked example:
"require tests to pass before merge" → grep for `gh pr merge`, direct merge steps, and CI jobs
that merge without the new gate.

CLASSIFY each existing call site into exactly one bucket:
- MIGRATED — the plan already updates this site to satisfy the new prohibition.
- EXEMPTED — the plan (or an explicit rationale) carves this site out of the prohibition.
- UNCOVERED — the site performs the outlawed behavior and the plan neither migrates nor exempts
  it. Each UNCOVERED site is the finding: the plan will start rejecting it with no migration path.

PASS when every existing call site is MIGRATED or EXEMPTED (or there are none). Report each
UNCOVERED site with its location as the grounded evidence.

FAIL-OPEN (abstain-with-coverage): if the outlawed behavior cannot be reduced to a checkable grep
pattern, or the repository tools cannot enumerate its call sites, ABSTAIN — record the prohibition
as covered-but-unenumerable rather than asserting an ungroundable gap. Do not fabricate call sites.
