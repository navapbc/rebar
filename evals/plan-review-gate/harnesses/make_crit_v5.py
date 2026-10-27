import json
crit = json.load(open('criteria_v4.json'))
have = {c['id'] for c in crit}

add = [
 {"id":"T1","exec":"AGENT","facet":"overlay-priorart","routing":"overlay",
  "trigger":"any of the 6 bright-lines: external integration / unfamiliar dependency / security-auth / novel pattern / perf-scalability / migration (deterministic prior + agent-judgment)",
  "name":"Prior-art / novel-architecture justification [overlay]",
  "scenario":("OVERLAY — apply when the plan crosses a bright-line (external integration, unfamiliar dependency, "
   "security/auth, a novel architectural pattern, a performance/scalability target, or a migration). Tool-grounded "
   "where possible (web/codebase). Checks: (a) is there relevant PRIOR ART the plan should consider before "
   "committing, or is it reinventing/repackaging something that exists? (b) for a novel pattern: is the novelty "
   "justified vs an established approach (anti-repackaging, Rule-of-Three)? (c) are unverified capability assertions "
   "('library supports X') resolved? SEVERITY: a novel architecture chosen with no consideration of prior art = MAJOR. "
   "ANTI-FP: a well-trodden pattern needs no prior-art search; not-applicable when no bright-line fires.")},
 {"id":"T6","exec":"1-TURN","facet":"overlay-ux","routing":"overlay",
  "trigger":"new user-facing UI (deterministic UI-keyword count ≥3, or classifier when ambiguous); LLM-confirmed",
  "name":"UX non-happy-path [overlay]",
  "scenario":("OVERLAY — apply only if the plan introduces a user-facing interaction surface; else PASS not-applicable. "
   "Checks: (a) criticality — are the highest-stakes interactions named? (b) non_happy_path — validation/timeout/empty/"
   "partial-data/error states handled, not just the happy path? (c) flow_entry_exit — entry plus both success and "
   "abandon exit points covered? SEVERITY: a new interactive flow with only the happy path = MAJOR. ANTI-FP: "
   "not-applicable for backend/infra/data work.")},
 {"id":"T7","exec":"1-TURN","facet":"overlay-docs","routing":"overlay",
  "trigger":"new pattern/contract/config, OR touches existing documented behavior (deterministic doc/ADR keywords + LLM)",
  "name":"Documentation [overlay]",
  "scenario":("OVERLAY — apply when the plan introduces something that needs documenting or invalidates existing docs; "
   "else PASS not-applicable. Checks: (a) NEW-needed — a new pattern/contract/config/CLI gets a doc/ADR? (b) "
   "INVALIDATED — does the change make existing docs/references stale (deleted/renamed artifacts still referenced)? "
   "(c) not-excessive / navigable — large docs have structure; no hot-path instruction-bloat. SEVERITY: a new "
   "architectural decision with no ADR/doc, or a change that strands stale references = MAJOR. ANTI-FP: trivial/"
   "internal changes need no doc.")},
 {"id":"T8","exec":"AGENT","facet":"overlay-llm","routing":"overlay",
  "trigger":"plan defines an LLM/agent system: prompts, sub-agents, reviewers, output schemas, enums (LLM-routed — a keyword trigger is high-FP)",
  "name":"LLM / prompt structural-completeness probe [overlay]",
  "scenario":("OVERLAY — apply when the plan defines an LLM/agent system (prompts, sub-agents, reviewers, output "
   "schemas, enums). Probe (tool-grounded) for STRUCTURAL GAPS a generic checklist misses: (a) a schema/enum "
   "referenced but whose value vocabulary is never defined; (b) a processing protocol/decision rule referenced but "
   "not co-located with the schema that needs it; (c) a counter/state increment with ambiguous placement; (d) an "
   "unspecified fallback for an incomplete/failed sub-step; (e) instruction-locality / pink-elephant antipatterns. "
   "Use Grep/Read to confirm referenced agents/skills/enums exist and are fully specified. Report each PROVEN gap "
   "with evidence. SEVERITY: an undefined-but-referenced enum/protocol an executor needs = MAJOR. ANTI-FP: cite "
   "concrete evidence; this is the overlay that recovers the structural-gap signal a generic checklist misses.")},
 {"id":"T9","exec":"1-TURN","facet":"overlay-sharedstate","routing":"overlay",
  "trigger":"plan introduces or mutates shared/global state, a cache, a singleton, a config key, or a lifecycle (deterministic prior + LLM)",
  "name":"Shared-state lifecycle [overlay]",
  "scenario":("OVERLAY — apply when the plan introduces or mutates shared/global state (a cache, singleton, config key, "
   "shared file/record, or a stateful lifecycle); else PASS not-applicable. Check the full CREATE / UPDATE / CONSUME / "
   "RETIRE lifecycle: (a) who creates the state and when? (b) update concurrency / ownership clear? (c) consumers "
   "enumerated and tolerant of its absence/staleness? (d) is there a RETIRE/cleanup path, or does it leak/accumulate? "
   "SEVERITY: shared state with no defined ownership or no retirement path = MAJOR. ANTI-FP: not-applicable for purely "
   "local/stateless changes.")},
]
for a in add:
    if a['id'] not in have:
        crit.append(a)
json.dump(crit, open('criteria_v5.json', 'w'), indent=1)
print('criteria_v5:', len(crit), 'criteria; added:', [a['id'] for a in add if a['id'] not in have])
print('ids:', [c['id'] for c in crit])
