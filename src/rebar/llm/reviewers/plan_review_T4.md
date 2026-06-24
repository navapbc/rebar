---
schema_version: 1
title: Compat / destructiveness as an explicit justified choice [overlay]
description: Plan-review overlay-compat criterion T4 (1-TURN). The rubric the Pass-1
  finder applies; routing in criteria_routing.json.
execution_mode: single_turn
category: plan-review-criterion
dimension: overlay-compat
---
OVERLAY — apply when the plan changes existing behavior, an interface/schema/data shape, or performs a destructive/irreversible operation; else PASS not-applicable. BIDIRECTIONAL check: (a) UNACKNOWLEDGED breakage — does the plan change/remove something consumers rely on without acknowledging the break, an expand-contract sequence, or a rollback path? (b) GRATUITOUS compat — does it add backward-compat shims, feature flags, or version branches that aren't warranted? (c) is a destructive/irreversible step an EXPLICIT, justified choice (not incidental)? SEVERITY: unacknowledged breaking change with no migration/rollback = MAJOR. ANTI-FP: a purely additive change is not-applicable; an explicitly justified breaking change with a migration is fine. The REMEDY for a destructive/breaking change is an explicit ROLLBACK / back-out plan or expand-contract sequencing — checking only that breakage is *acknowledged* is insufficient; require the reversibility mechanism.
