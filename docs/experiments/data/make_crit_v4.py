import json
crit = json.load(open('criteria_v3.json'))

# 1. EXP was T2 all along — rename it back.
for c in crit:
    if c['id'] == 'EXP':
        c['id'] = 'T2'
        c['name'] = 'Empirical probe (red->green / spike) [overlay]'
        c['facet'] = 'overlay-empirical'
        c['trigger'] = 'complex/novel plan with an unvalidated assumption (LLM-routed)'
        c['routing'] = 'overlay'

# 2. Restore the criteria that were dropped from the set and never reconciled (grounded in DSO catalog).
restored = [
 {"id":"T3","exec":"AGENT","facet":"overlay-feasibility","routing":"overlay",
  "trigger":"external integration / first-time-internal-platform / unverified capability (LLM-routed)",
  "name":"Integration feasibility [overlay]",
  "scenario":("OVERLAY — apply only when the plan integrates an external API/CLI/service/library or asserts a capability "
   "it has not used before; else PASS not-applicable. Binary checks (tool-grounded where possible): (a) technical_feasibility "
   "— is the integration achievable as described, or is there a capability gap? (b) for a CLI/API: do the named subcommands/"
   "endpoints actually exist (verify against --help / docs) — MATCH / MISMATCH / UNVERIFIED; (c) auth/HTTPS preconditions "
   "stated; (d) a critical capability gap should route to a SPIKE before committing the full plan. SEVERITY: an asserted-but-"
   "unverified external capability the plan depends on = MAJOR. ANTI-FP: verify before asserting a mismatch; an internal, "
   "already-used integration is not-applicable.")},
 {"id":"T4","exec":"1-TURN","facet":"overlay-compat","routing":"overlay",
  "trigger":"P7 destructive/irreversible sniff, or a change to existing behavior/interface/schema/data (deterministic prior + LLM)",
  "name":"Compat / destructiveness as an explicit justified choice [overlay]",
  "scenario":("OVERLAY — apply when the plan changes existing behavior, an interface/schema/data shape, or performs a "
   "destructive/irreversible operation; else PASS not-applicable. BIDIRECTIONAL check: (a) UNACKNOWLEDGED breakage — does the "
   "plan change/remove something consumers rely on without acknowledging the break, an expand-contract sequence, or a rollback "
   "path? (b) GRATUITOUS compat — does it add backward-compat shims, feature flags, or version branches that aren't warranted? "
   "(c) is a destructive/irreversible step an EXPLICIT, justified choice (not incidental)? SEVERITY: unacknowledged breaking "
   "change with no migration/rollback = MAJOR. ANTI-FP: a purely additive change is not-applicable; an explicitly justified "
   "breaking change with a migration is fine.")},
 {"id":"T5d","exec":"1-TURN","facet":"overlay-a11y","routing":"overlay",
  "trigger":"new user-facing UI (deterministic UI-keyword count; null otherwise)",
  "name":"Accessibility [overlay]",
  "scenario":("OVERLAY — apply only if the plan introduces new user-facing UI; else PASS not-applicable. Binary checks: "
   "(a) wcag_compliance — does the scope address WCAG 2.1 AA with observable a11y done-definitions (keyboard, screen-reader, "
   "contrast)? (b) inclusive_ux — reduced motion, keyboard-only, screen-reader, touch-target sizing, not color-alone/mouse-only. "
   "SEVERITY: a new interactive surface with no keyboard nav = MAJOR — cite the WCAG criterion. ANTI-FP: not-applicable for "
   "backend/infra/data work.")},
 {"id":"COH","exec":"1-TURN","facet":"coherence","routing":"base",
  "name":"Cross-section coherence pass (cross-cutting)",
  "scenario":("CROSS-CUTTING coherence pass (distinct from E1's criteria<->description check): a single structured scan for "
   "CONTRADICTIONS BETWEEN SECTIONS of the plan — e.g. the testing strategy contradicts the decomposition; the sequencing "
   "contradicts the declared dependencies; the context/problem contradicts the success criteria; an approach choice contradicts "
   "a stated constraint. One pass, not a debate. SEVERITY: a contradiction that would send the implementer in two directions = "
   "MAJOR. ANTI-FP: only flag genuine cross-section contradictions, not within-section nitpicks (those belong to E1/E2).")},
]
have = {c['id'] for c in crit}
for r in restored:
    if r['id'] not in have:
        crit.append(r)

json.dump(crit, open('criteria_v4.json', 'w'), indent=1)
print("criteria_v4:", len(crit), "criteria")
print("  T2 (was EXP):", any(c['id'] == 'T2' for c in crit))
print("  restored:", [r['id'] for r in restored])
print("  ids:", [c['id'] for c in crit])
