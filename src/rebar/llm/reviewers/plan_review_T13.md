---
schema_version: 1
title: Behavioral-obligation consumer scan
description: Plan-review overlay T13 (AGENT, LLM-routed). Gap-report G-5. The rubric
  the Pass-1 finder applies for a plan that newly IMPOSES an obligation — forbids a
  previously-permitted action, or adds an obligation existing sites now owe;
  routing in criteria_routing.json. See ADR 0034.
execution_mode: agentic
category: plan-review-criterion
dimension: overlay-prohibition
---
OVERLAY — apply only when the plan NEWLY IMPOSES an obligation, in either form. (1) A
PROHIBITION: it introduces an enforcement/gate that will start rejecting something that used to
be allowed. (2) An ADDITION: it creates a new obligation — a contract, invariant, schema field,
required argument, config key, changed default, or a second site that must stay in step — that
existing sites now owe. Trigger lexicon — prohibition: "block", "reject", "require … before",
"enforce", "must pass", "cannot merge until", "deny", "fail the build if"; addition: "adds a
required …", "now requires", "callers must", "every X must now", "must stay in sync/in step",
"new contract/invariant/schema field". If the plan introduces no new obligation of either form,
PASS as not-applicable.

ENUMERATE THE INVISIBLE AFFECTED SET. A new obligation silently breaks the existing sites that
owe it — nothing in the remaining plan references them, so they are invisible unless enumerated.
Translate the obligation into concrete grep patterns over EXISTING sites that owe it — for a
prohibition, the call sites of the behavior being outlawed; for an addition, the surfaces and
consumers that must discharge the new obligation — then Grep/Read to find them. Worked examples:
"require tests to pass before merge" → grep for `gh pr merge`, direct merge steps, and CI jobs
that merge without the new gate; "add a required `--class` argument to the CLI" → grep the other
entry surfaces that build the same request (MCP tool schema, NDJSON import) for that field.

CLASSIFY each existing site into exactly one bucket:
- MIGRATED — the plan already updates this site to satisfy the new obligation.
- EXEMPTED — the plan (or an explicit rationale) carves this site out of the obligation.
- UNCOVERED — the site owes the obligation and the plan neither migrates nor exempts it. Each
  UNCOVERED site is the finding: a prohibition will start rejecting it with no migration path;
  an addition leaves it silently out of step.

PASS when every existing site is MIGRATED or EXEMPTED (or there are none). Report each
UNCOVERED site with its location as the grounded evidence.

FAIL-OPEN (abstain-with-coverage): if the obligation cannot be reduced to a checkable grep
pattern, or the repository tools cannot enumerate the sites that owe it, ABSTAIN — record the
obligation as covered-but-unenumerable rather than asserting an ungroundable gap. Do not
fabricate sites.
