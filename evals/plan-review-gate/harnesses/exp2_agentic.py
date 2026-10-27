import json, os, time, threading, subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import anthropic
import harness as h

TMP = h.TMP
RUNS = os.path.join(TMP, 'exp2_agentic.jsonl')
MODEL = "claude-sonnet-4-6"
MAX_ITERS = 20
client = anthropic.Anthropic()

# Three AGENT-tier (tool-using) criteria, fully specified with the codebase evidence they must gather.
AGENT_CRIT = {
 "E4": "E4 — Assumption/premise verification (AGENT). Scan the plan for assertions that something already exists in the codebase or behaves a certain way — hedging triggers ('assume','likely','should be','we'll get') and confident-assertion triggers ('loaded from','provided by','is fetched from','reuses','already in repo','already does'). For EACH such assertion you MUST run a Grep or Read to confirm it against the actual code — cached/training knowledge is NOT a substitute. Treat an unverifiable assertion as a gap (fail-closed: absent evidence => the assertion is unverified, do not give benefit of the doubt). Report each assertion with its verification result.",
 "G1G2": "G1G2 — Edit-set / scope accuracy (AGENT). For every file, module, or symbol the plan names as something it will touch or reuse, run Glob/Grep to confirm it actually exists. Then for changes of behavior, Grep for consumers/callers OUTSIDE the artifact's own directory and check whether the plan accounts for updating them (missing consumers = a scope gap). Flag named edit targets that do not exist (possible hallucination) and unenumerated consumers. Report raw evidence (path + matching line).",
 "A1": "A1 — Anti-slop / over-engineering / NIH (AGENT). For each new abstraction, helper, config surface, or capability the plan proposes, Grep the codebase to check: (Rule-of-Three) does a proposed abstraction have >=3 existing call sites, or is it premature? (NIH) does the plan rebuild functionality that already exists in the codebase or an imported dependency — Grep for it? (config proliferation) does a config key already exist that captures this toggle? Every finding MUST cite concrete codebase evidence (a grep hit), never a hypothetical. Report what you found.",
}

TOOLS = [
 {"name": "grep", "description": "Search file contents with a regex (ripgrep). Returns matching lines with file:line.",
  "input_schema": {"type": "object", "properties": {
      "pattern": {"type": "string"}, "path": {"type": "string", "description": "optional subdir/glob to scope"}},
      "required": ["pattern"]}},
 {"name": "read_file", "description": "Read a file (optionally a line range). Returns the contents.",
  "input_schema": {"type": "object", "properties": {
      "path": {"type": "string"}, "start": {"type": "integer"}, "limit": {"type": "integer"}},
      "required": ["path"]}},
 {"name": "glob", "description": "List files matching a glob pattern under the repo root.",
  "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
 {"name": "rebar", "description": "Read-only query of the rebar ticket store: the LIVE dependency graph, hierarchy, and linked artifacts (NOT a fed snapshot). Use 'show <id>' for a ticket incl. its deps/links/comments, 'deps <id>' for blockers/children, 'ready'/'next-batch <epic>' for unblocked work, 'search <query>' for related tickets and session logs.",
  "input_schema": {"type": "object", "properties": {
      "subcommand": {"type": "string", "enum": ["show", "list", "deps", "ready", "next-batch", "search"]},
      "args": {"type": "string", "description": "args after the subcommand, e.g. a ticket id or a search query"}},
      "required": ["subcommand"]}},
 {"name": "submit_review", "description": "Submit the per-criterion review after gathering evidence.",
  "input_schema": h.TOOL[0]["input_schema"]},
]

def run_tool(name, inp, repo_root):
    try:
        if name == "grep":
            pat = inp["pattern"]; scope = inp.get("path", "")
            cmd = ["rg", "-n", "--no-heading", "-S", "-m", "40", pat]
            target = os.path.join(repo_root, scope) if scope else repo_root
            cmd.append(target)
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
            out = out.replace(repo_root + "/", "")
            return out[:20000] if out else "(no matches)"
        if name == "read_file":
            p = os.path.join(repo_root, inp["path"]) if not inp["path"].startswith("/") else inp["path"]
            if not os.path.isfile(p):
                return f"(no such file: {inp['path']})"
            lines = open(p, errors="replace").read().splitlines()
            s = max(0, inp.get("start", 1) - 1); lim = inp.get("limit", 120)
            return "\n".join(lines[s:s + lim])[:20000]
        if name == "glob":
            import glob as G
            hits = G.glob(os.path.join(repo_root, "**", inp["pattern"]), recursive=True)
            hits = [hh.replace(repo_root + "/", "") for hh in hits][:60]
            return "\n".join(hits) if hits else "(no matches)"
        if name == "rebar":
            sub = inp.get("subcommand", "")
            allow = {"show", "list", "deps", "ready", "next-batch", "search"}
            if sub not in allow:
                return f"(rebar: subcommand '{sub}' not allowed; read-only set is {sorted(allow)})"
            args = (inp.get("args") or "").split()
            out = subprocess.run(["rebar", sub, *args], capture_output=True, text=True,
                                 timeout=30, cwd=repo_root).stdout
            return out[:20000] if out else "(no output)"
    except Exception as e:
        return f"(tool error: {e})"
    return "(unknown tool)"

SYS = h.SYSTEM + "\n\nYou have READ-ONLY tools: the FILE SYSTEM (grep, read_file, glob) AND rebar (show/list/deps/ready/next-batch/search) — the LIVE ticket dependency graph, hierarchy, and linked artifacts. These two families TOGETHER reach any artifact the ticket references: linked session logs and related/blocking tickets via `rebar show`/`deps`, and docs/experiments reports or research files via read_file/glob. USE them to gather concrete evidence BEFORE judging — you must not answer from assumption. For a CONTAINER or CROSS-TICKET criterion, read the LIVE dependency graph with rebar (e.g. `deps <child>`) rather than trusting a fed snapshot: a declared depends_on edge or a child's coverage you don't see in the text is in the graph. Be EFFICIENT: a handful of targeted tool calls (aim for under 8 total), then call submit_review. Do not exhaustively explore."

def run_agentic(plan_title, plan_text, crit_id, repo_root):
    # cache the stable system+plan prefix
    system = [{"type": "text", "text": SYS},
              {"type": "text", "text": f"# Plan under review\nTitle: {plan_title}\n\n## Plan\n{plan_text}",
               "cache_control": {"type": "ephemeral"}}]
    user = (f"## Apply this codebase-grounded criterion\n{AGENT_CRIT[crit_id]}\n\n"
            f"Gather evidence with the tools (under ~8 calls), then call submit_review with one entry for criterion id '{crit_id}'.")
    messages = [{"role": "user", "content": user}]
    t0 = time.time(); tin = tout = cr = cw = tool_calls = iters = 0
    findings = None
    for it in range(MAX_ITERS):
        iters += 1
        last = it >= MAX_ITERS - 1  # force a verdict on the final iteration
        # cache the running conversation tail
        if isinstance(messages[-1]["content"], list):
            for blk in messages[-1]["content"]:
                if isinstance(blk, dict):
                    blk["cache_control"] = {"type": "ephemeral"}
        kw = dict(model=MODEL, max_tokens=16000, system=system, tools=TOOLS, messages=messages)
        if last:
            kw["tool_choice"] = {"type": "tool", "name": "submit_review"}
        resp = client.messages.create(**kw)
        tin += resp.usage.input_tokens; tout += resp.usage.output_tokens
        cr += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        cw += getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
        # strip transient cache markers we added so history stays byte-stable
        if isinstance(messages[-1]["content"], list):
            for blk in messages[-1]["content"]:
                if isinstance(blk, dict):
                    blk.pop("cache_control", None)
        tus = [b for b in resp.content if b.type == "tool_use"]
        if not tus:
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []; done = False
        for b in tus:
            if b.name == "submit_review":
                findings = b.input.get("criteria", []); done = True
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": "ok"})
            else:
                tool_calls += 1
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": run_tool(b.name, b.input, repo_root)})
        messages.append({"role": "user", "content": results})
        if done:
            break
    return {"findings": findings or [], "latency_s": time.time() - t0, "in_tok": tin, "out_tok": tout,
            "cache_read": cr, "cache_write": cw, "tool_calls": tool_calls, "iters": iters}

# Targets: dogfood epic 5fd2 vs the rebar repo; one DSO task vs the DSO repo
REBAR = "/Users/joeoakhart/rebar"
DSO = os.path.expanduser("~/digital-service-orchestra")
epic = json.load(open(os.path.join(TMP, 'epic.json')))
dso_plans = json.load(open(os.path.join(TMP, 'plans_dso.json')))
TARGETS = [
    {"key": "dogfood_epic", "title": epic['title'], "plan": epic['description'], "repo": REBAR},
    {"key": "dso_complex", "title": dso_plans['complex_leaf']['title'], "plan": dso_plans['complex_leaf']['plan'], "repo": DSO},
]
REPEATS = 3

lock = threading.Lock()
def run_job(tgt, cid, rep):
    for attempt in range(3):
        try:
            r = run_agentic(tgt['title'], tgt['plan'], cid, tgt['repo'])
            rec = {'ticket_key': tgt['key'], 'criterion': cid, 'repeat': rep,
                   'findings': r['findings'], 'latency_s': r['latency_s'], 'in_tok': r['in_tok'],
                   'out_tok': r['out_tok'], 'cache_read': r['cache_read'], 'cache_write': r['cache_write'],
                   'tool_calls': r['tool_calls'], 'iters': r['iters']}
            with lock:
                open(RUNS, 'a').write(json.dumps(rec) + '\n')
            return True
        except Exception as e:
            if attempt == 2:
                with lock:
                    open(RUNS, 'a').write(json.dumps({'ERROR': str(e), 'criterion': cid, 'ticket_key': tgt['key']}) + '\n')
                return False
            time.sleep(3)

if __name__ == '__main__':
    jobs = [(t, c, r) for t in TARGETS for c in AGENT_CRIT for r in range(REPEATS)]
    print(f"EXP2 agentic: {len(AGENT_CRIT)} tool-using criteria x {len(TARGETS)} targets x {REPEATS} = {len(jobs)} agent runs")
    open(RUNS, 'w').close()
    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(run_job, *j) for j in jobs]
        for fu in as_completed(futs):
            done += 1
            print(f"  {done}/{len(jobs)} agent runs done", flush=True)
    print("ALL DONE", len(jobs))
