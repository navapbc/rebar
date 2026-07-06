---
schema_version: 1
title: CI-trigger / release-infrastructure coverage audit
description: Plan-review overlay T14 (AGENT, LLM-routed). Gap-report G-10. The rubric
  the Pass-1 finder applies for a plan that adds a git ref pattern / event source
  / schedule, or new release-time-exercised infrastructure; routing in criteria_routing.json.
  See ADR 0034.
execution_mode: agentic
category: plan-review-criterion
dimension: overlay-citrigger
---
OVERLAY — apply only when the plan introduces a NEW git ref pattern (branch namespace, tag/merge
ref), a new event source or schedule, or otherwise changes what CI fires on; OR adds new
release-time-exercised infrastructure (a new package, a new CI job, a plugin entry point). If the
plan does none of these, PASS as not-applicable.

WORKFLOW-TRIGGER FILTER AUDIT. A new ref/event pattern silently fails to fire when existing
workflow trigger filters do not include it (the real defect: `branches: [main]` silently skipping
per-story PRs). Enumerate the repository's workflow files (Grep/Read `.github/workflows/*.yml`)
and, for the new pattern, classify EACH workflow's trigger filter into exactly one bucket:
- INCLUDED — the workflow's trigger filter matches the new pattern, so it will fire.
- EXCLUDED — the workflow's trigger filter explicitly does NOT match the new pattern, so it will
  silently skip. Each EXCLUDED workflow that SHOULD fire is the finding.
- NO_FILTER — the workflow has no ref/event filter, so the new pattern is trivially covered.
Require an affirmative per-workflow classification — do not assume coverage.

RELEASE-INFRASTRUCTURE SIBLING. A release-time-exercised change (new package, new CI job, plugin
entry point) must be reflected in the release script's dependency graph — this is an INTERNAL
script dependency, not an external-service shape, so the generic external-outcome classifier misses
it. Flag a release-infra change the release process does not account for.

PASS when every workflow is INCLUDED or NO_FILTER (or affirmatively EXEMPTED with rationale) and
release infra is accounted for.

FAIL-OPEN (abstain-with-coverage): if a workflow's trigger syntax is unknown/unparseable, or the CI
system is one the tools cannot read, ABSTAIN for that workflow — record it as covered-but-unverified
rather than asserting an EXCLUDED gap you cannot ground. Fail open, never fail closed on unknown CI.
