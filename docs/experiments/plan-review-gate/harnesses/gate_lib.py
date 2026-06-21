"""gate_lib — the reusable plan-review substrate for the finalize experiments.

Consolidates what was scattered across exp1/exp2/round4/retune into one hardened module:
  - robust_findings()   : item-E parse hardening — never crash on a missing tool_use /
                          empty criteria / malformed entry; coerce to the verdict schema
                          and report a parse_status so the caller can retry or mark INDETERMINATE.
  - SYSTEM              : harness SYSTEM + the DECISIVENESS LEVER (round-5 scorecard fix for
                          AMBIGUOUS-on-clean: don't hedge merely for lack of live-code access).
  - applies()          : declarative proportionate-scrutiny filter reading criteria_v7's
                          applies_at{} (replaces retune.py's hard-coded LEAF_ONLY/ALL_LEVEL).
  - chunk_by_facet()   : base_chunk(model) x size_factor(ticket) within the single-turn tier.
  - det_overlays()     : deterministic low-FP overlay triggers (round-4 + T10/T11/T12).
  - single_turn()/agent(): hardened call wrappers (cached prefix, retries, robust parse).

This is experiment scaffolding, not production code (see README). It is, however, written to
mirror the orchestrator the epic specifies, so the implementation can lift the shapes.
"""
import json, os, re, time
import anthropic
import harness as h   # reuse SYSTEM/TOOL/TMP and the cached-call conventions
import exp2_agentic as e2

TMP = h.TMP
client = anthropic.Anthropic()
VALID_VERDICT = {"PASS", "AMBIGUOUS", "FAIL"}
VALID_SEV = {"none", "minor", "major", "critical"}

CRIT_V7 = "/Users/joeoakhart/rebar/docs/experiments/plan-review-gate/criteria/criteria_v7.json"

# ---- DECISIVENESS LEVER (round-5 scorecard fix) ----------------------------
# The single-turn criteria hedged AMBIGUOUS on clean-but-terse good plans purely because
# they couldn't run the code. That inflates the precision metric. Instruct the reviewer to
# reserve AMBIGUOUS for genuine under-specification, not mere lack of live-code access.
DECISIVENESS = (
    "\n\nDECISIVENESS: Reserve AMBIGUOUS for cases where the PLAN ITSELF genuinely under-specifies "
    "the criterion, or where a specific codebase fact is load-bearing AND unknowable from the plan text. "
    "Do NOT return AMBIGUOUS merely because you cannot run or read the live code: if the plan's own text "
    "affirmatively satisfies the criterion, return PASS; if the plan's own text shows the defect, return FAIL. "
    "A well-specified plan you simply can't execute is a PASS, not an AMBIGUOUS."
)
SYSTEM = h.SYSTEM + DECISIVENESS


# ---- item E: robust parsing ------------------------------------------------
def robust_findings(resp, expected_ids=None):
    """Extract per-criterion findings from a messages response, defensively.

    Returns (findings, status) where status in {ok, no_tool_use, empty, repaired, partial}.
    Every returned finding is coerced to the schema: criterion_id present, verdict in
    VALID_VERDICT (default AMBIGUOUS), severity in VALID_SEV (default by verdict),
    location/finding/suggested_edit strings, confidence float in [0,1].
    """
    raw = None
    for b in resp.content:
        if getattr(b, "type", None) == "tool_use":
            inp = b.input if isinstance(b.input, dict) else {}
            raw = inp.get("criteria", None)
            break
    if raw is None:
        return [], "no_tool_use"
    if not isinstance(raw, list):
        raw = [raw] if isinstance(raw, dict) else []
    repaired = False
    out = []
    for item in raw:
        if not isinstance(item, dict):
            repaired = True
            continue
        cid = item.get("criterion_id") or item.get("id") or item.get("criterion")
        if not cid:
            repaired = True
            continue
        v = str(item.get("verdict", "")).upper().strip()
        if v not in VALID_VERDICT:
            v = "AMBIGUOUS"; repaired = True
        sev = str(item.get("severity", "")).lower().strip()
        if sev not in VALID_SEV:
            sev = "none" if v == "PASS" else ("major" if v == "FAIL" else "minor")
            repaired = True
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5; repaired = True
        conf = max(0.0, min(1.0, conf))
        out.append({
            "criterion_id": str(cid),
            "verdict": v, "severity": sev,
            "location": str(item.get("location", "") or ""),
            "finding": str(item.get("finding", "") or ""),
            "suggested_edit": str(item.get("suggested_edit", "") or ""),
            "confidence": conf,
        })
    if not out:
        return [], "empty"
    status = "repaired" if repaired else "ok"
    if expected_ids is not None:
        got = {f["criterion_id"] for f in out}
        missing = set(expected_ids) - got
        if missing:
            # synthesize an INDETERMINATE entry for each missing id so downstream joins don't drop it
            for mid in sorted(missing):
                out.append({"criterion_id": mid, "verdict": "AMBIGUOUS", "severity": "minor",
                            "location": "", "finding": "(no verdict returned by model)",
                            "suggested_edit": "", "confidence": 0.0})
            status = "partial"
    return out, status


def load_criteria(path=CRIT_V7):
    return json.load(open(path))


# ---- declarative proportionate scrutiny (replaces retune.py hard-coding) ----
def is_test_task(plan):
    p = (plan or "").lower()
    return (("testing mode" in p and ("red" in p or "green" in p))
            or "red task" in p or "red test task" in p
            or ("write" in p and "test" in p and "for the" in p and len(p) < 1200))


def is_mechanical_leaf(plan, ttype):
    p = (plan or "").lower()
    sig = any(w in p for w in ("refactor", "rename", "move ", "extract ", "dep-bump",
                               "dependency bump", "bump ", "typo", "lint", "format",
                               "gitignore", "comment", "docstring"))
    return ttype == "task" and sig


def applies(crit, level, has_children=False, ttype=None, plan=""):
    """Should `crit` run for a ticket at `level` (epic|story|task) of type `ttype`?

    Reads crit['applies_at'] = {levels[], container_only, suppress_types[], suppress_when[]}.
    Trigger-gating (does the work even have surface for an overlay) is a SEPARATE step
    (det_overlays / the LLM router); this is purely the level/type filter.
    """
    ap = crit.get("applies_at")
    if ap is None:
        return True
    if ttype and ttype in ap.get("suppress_types", []):
        return False
    if level not in ap.get("levels", ["epic", "story", "task"]):
        return False
    if ap.get("container_only") and not has_children:
        return False
    for cond in ap.get("suppress_when", []):
        if cond == "test_task" and is_test_task(plan):
            return False
        if cond == "mechanical_leaf" and is_mechanical_leaf(plan, ttype):
            return False
    return True


# ---- chunking: base_chunk(model) x size_factor(ticket), single-turn tier -----
def base_chunk(model):
    m = model.lower()
    if "opus" in m: return 12
    if "sonnet" in m: return 6
    return 3  # haiku / local


def size_factor(ticket_size):
    return 0.5 if ticket_size in ("large", "epic", "has_children") else 1.0


def chunk_by_facet(crits, model="claude-sonnet-4-6", ticket_size="moderate"):
    """Pack same-facet single-turn criteria into chunks of base_chunk x size_factor (clamp [2,n])."""
    n = max(2, int(round(base_chunk(model) * size_factor(ticket_size))))
    by_facet = {}
    for c in crits:
        by_facet.setdefault(c.get("facet", "misc"), []).append(c)
    ordered = [c for f in by_facet for c in by_facet[f]]
    return [ordered[i:i + n] for i in range(0, len(ordered), n)]


# ---- deterministic overlay triggers (round-4 DET_RULES + T10/T11/T12) -------
DET_RULES = {
 "T1":  r"\b(api|sdk|third.?party|integrat|oauth|credential|token|secret|migrat|backward.?compat|deprecat|novel|new (library|package|dependency|architecture|pattern))\b",
 "T5a": r"\b(latency|throughput|performance|scal|n\+1|batch|loop|cache|memory|compute|llm call|concurren)\b",
 "T5b": r"\b(retry|timeout|failover|idempoten|error.handling|circuit|fail.open|fail.clos|graceful|external (api|service)|write op)\b",
 "T5c": r"\b(auth|oauth|credential|token|secret|pii|endpoint|encrypt|signature|sign(ing|ed)?|access control|forgery|replay)\b",
 "T5d": r"\b(ui|button|form|screen|page|modal|dashboard|keyboard|wcag|aria|accessib|color|contrast)\b",
 "T5e": r"\b(refactor|coupl|abstraction|cross.component|new (pattern|interface)|config|adr|maintainab|module boundary)\b",
 "T6":  r"\b(ui|button|form|screen|user.facing|non.happy|empty state|validation|error message|flow)\b",
 "T7":  r"\b(\bdoc\b|docs|readme|claude\.md|adr|guide|documentation)\b",
 "T8":  r"\b(agent|prompt|llm|sub.?agent|model|reviewer|skill|instruction|schema|enum)\b",
 "T9":  r"\b(shared|global|singleton|state|cache|config key|lifecycle|concurren)\b",
 "T10": r"\b(terraform|\.tf\b|tfvars|cloudformation|cdk|pulumi|ansible|kubernetes|k8s|helm|iam|vpc|security group|provision|\bs3\b|\brds\b|\bec2\b|lambda|aws_|gcp|azure)\b",
 "T11": r"\b(migrat|backfill|schema change|alter table|add column|expand.contract|\bddl\b|reindex|data shape|persisted format)\b",
 "T12": r"\b(deploy|rollout|canary|feature flag|production traffic|\bramp\b|rollback|staged|blue.green)\b",
}


def det_overlays(plan):
    p = (plan or "").lower()
    return {ov: bool(re.search(rx, p, re.I)) for ov, rx in DET_RULES.items()}


# ---- checklist-aware user prompt -------------------------------------------
def crit_block(c):
    line = f"- [{c['id']}] {c['name']} — {c['scenario']}"
    cl = c.get("checklist")
    if cl:
        line += "\n  Binary checks:" + "".join(f"\n    - ({i['key']}) {i['check']}" for i in cl)
    return line


def build_user(chunk):
    lines = ["## Review criteria to apply (apply EACH; one verdict entry per id)"]
    for c in chunk:
        lines.append("\n" + crit_block(c))
    lines.append("\nCall submit_review with exactly one entry per criterion id above.")
    return "\n".join(lines)


# ---- hardened call wrappers -------------------------------------------------
def single_turn(title, plan, chunk, model="claude-sonnet-4-6", extra="", retries=3):
    system = [{"type": "text", "text": SYSTEM},
              {"type": "text", "text": f"# Ticket plan under review\nTitle: {title}\n{extra}\n## Plan\n{plan}",
               "cache_control": {"type": "ephemeral"}}]
    ids = [c["id"] for c in chunk]
    last_status = None
    for attempt in range(retries):
        try:
            t0 = time.time()
            r = client.messages.create(model=model, max_tokens=4000, system=system, tools=h.TOOL,
                                       tool_choice={"type": "tool", "name": "submit_review"},
                                       messages=[{"role": "user", "content": build_user(chunk)}])
            findings, status = robust_findings(r, expected_ids=ids)
            last_status = status
            if status in ("no_tool_use", "empty") and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1)); continue
            return {"findings": findings, "status": status, "lat": time.time() - t0,
                    "in": r.usage.input_tokens, "out": r.usage.output_tokens,
                    "cr": getattr(r.usage, "cache_read_input_tokens", 0) or 0}
        except Exception as e:
            if attempt == retries - 1:
                return {"findings": [], "status": f"error:{e}", "lat": 0, "in": 0, "out": 0, "cr": 0}
            time.sleep(2 * (attempt + 1))
    return {"findings": [], "status": last_status or "error", "lat": 0, "in": 0, "out": 0, "cr": 0}


def agent(title, plan, crit_id, repo_root, retries=2):
    """AGENT-tier call. Reuses exp2's tool loop, but parses with robust_findings."""
    for attempt in range(retries):
        try:
            r = e2.run_agentic(title, plan, crit_id, repo_root)
            # exp2 already returns findings; coerce them through the same hardening
            fake = type("R", (), {"content": [type("B", (), {"type": "tool_use",
                       "input": {"criteria": r["findings"]}})()]})()
            findings, status = robust_findings(fake, expected_ids=[crit_id])
            r["findings"], r["status"] = findings, status
            return r
        except Exception as e:
            if attempt == retries - 1:
                return {"findings": [], "status": f"error:{e}", "tool_calls": 0, "iters": 0,
                        "latency_s": 0, "in_tok": 0, "out_tok": 0, "cache_read": 0}
            time.sleep(3)


# exp2's AGENT_CRIT only specifies E4/G1G2/A1; extend with the v7 AGENT criteria so agent()
# can run them. The descriptor scenario is the source of truth for the rest.
def ensure_agent_crit(crits):
    for c in crits:
        if c.get("exec") == "AGENT" and c["id"] not in e2.AGENT_CRIT:
            e2.AGENT_CRIT[c["id"]] = f"{c['id']} — {c['name']}. {c['scenario']}"


if __name__ == "__main__":
    crits = load_criteria()
    ensure_agent_crit(crits)
    print(f"loaded {len(crits)} v7 descriptors; AGENT tier: {[c['id'] for c in crits if c.get('exec')=='AGENT']}")
    print("applies_at sanity:")
    for lvl in ("epic", "story", "task"):
        on = [c["id"] for c in crits if applies(c, lvl, has_children=(lvl != "task"), ttype=lvl)]
        print(f"  {lvl:5} (has_children={lvl!='task'}): {len(on)} criteria -> {on}")
    print("  bug (task):", [c["id"] for c in crits if applies(c, "task", ttype="bug")], "(expect [] — bugs exempt)")
    print("det_overlays on a terraform/migration plan:",
          {k: v for k, v in det_overlays("provision an S3 bucket via terraform; ALTER TABLE add column; canary rollout").items() if v})
