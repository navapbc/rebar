#!/usr/bin/env python3
"""E4 corpus sampler — pull a varied, NON-DSO ticket sample from two repos.

The round-4 applies_at routing was tuned on the DSO sample (calibrating against the test
set). E4 is the held-out check: does the suite + routing behave on a DIFFERENT ticket
population? We use two real, polyglot non-DSO corpuses, each dogfooding rebar:
  - rebar itself           (Python library/CLI/MCP)         -> /Users/joeoakhart/rebar
  - snap-oakhart-manual    (Rails / Ruby app)               -> ~/snap-oakhart-manual/snap-oakhart-manual

Selects a spread across type x level x complexity, preferring tickets with real plans
(>=200 chars, an Acceptance Criteria block), and tagging has_children (for G3/G4) and
overlay-relevant signals (infra/migration/UI/llm) so the run exercises the overlays.

Writes corpus_sample.json to TMP: [{repo, repo_root, id, type, level, has_children,
overlay_signals[], title, plan}].
"""
import json, os, subprocess, random
import harness as h

TMP = h.TMP
random.seed(424242)

REPOS = {
    "rebar": "/Users/joeoakhart/rebar",
    "snap":  os.path.expanduser("~/snap-oakhart-manual/snap-oakhart-manual"),
}
LEVEL = {"epic": "epic", "story": "story", "task": "task", "bug": "bug"}
PER_BUCKET = {"epic": 2, "story": 3, "task": 4, "bug": 2}  # target per repo

OVERLAY_SIG = {
    "infra": r"terraform|\.tf|cloudformation|cdk|pulumi|ansible|kubernetes|helm|iam|vpc|aws|gcp|azure|provision",
    "migration": r"migrat|backfill|schema|alter table|add column|ddl|reindex",
    "ui": r"\bui\b|button|form|screen|page|view|erb|component|dashboard|html|css",
    "llm": r"\bllm\b|prompt|agent|model|reviewer|anthropic|openai|embedding",
    "deploy": r"deploy|rollout|canary|feature flag|production|rollback",
}
import re
def signals(text):
    t = (text or "").lower()
    return [k for k, rx in OVERLAY_SIG.items() if re.search(rx, t)]


def load_all(repo_root):
    out = []
    for st in ("open", "in_progress", "closed"):
        r = subprocess.run(["rebar", "list", f"--status={st}"], capture_output=True, text=True, cwd=repo_root)
        try:
            out += json.loads(r.stdout or "[]")
        except json.JSONDecodeError:
            pass
    # dedupe by id
    seen, uniq = set(), []
    for t in out:
        if t["ticket_id"] not in seen and t.get("ticket_type") != "session_log":
            seen.add(t["ticket_id"]); uniq.append(t)
    return uniq


def pick(repo, repo_root):
    tickets = load_all(repo_root)
    parents = {t.get("parent_id") for t in tickets if t.get("parent_id")}
    buckets = {k: [] for k in PER_BUCKET}
    for t in tickets:
        tt = t.get("ticket_type")
        if tt not in buckets:
            continue
        plan = t.get("description") or ""
        if len(plan) < 200:        # need a real plan to review
            continue
        buckets[tt].append(t)
    chosen = []
    for tt, want in PER_BUCKET.items():
        cands = buckets[tt]
        # prefer ones with an AC block + overlay signal variety
        cands.sort(key=lambda t: (("## acceptance criteria" in (t["description"].lower())),
                                   len(signals(t["description"])), len(t["description"])), reverse=True)
        # take a spread: a few top, a couple random from the rest, for variety
        top = cands[:want]
        chosen += top
    recs = []
    for t in chosen:
        recs.append({
            "repo": repo, "repo_root": repo_root, "id": t["ticket_id"],
            "type": t["ticket_type"], "level": LEVEL[t["ticket_type"]],
            "has_children": t["ticket_id"] in parents,
            "overlay_signals": signals(t["description"] + " " + t["title"]),
            "title": t["title"], "plan": t["description"],
        })
    return recs, len(tickets)


if __name__ == "__main__":
    allrecs = []
    for repo, root in REPOS.items():
        recs, n = pick(repo, root)
        print(f"{repo}: {n} tickets scanned -> {len(recs)} sampled "
              f"({', '.join(sorted(set(r['type'] for r in recs)))})")
        for r in recs:
            print(f"    {r['type']:6} {r['id'][:9]} children={r['has_children']!s:5} "
                  f"sig={r['overlay_signals']} :: {r['title'][:64]}")
        allrecs += recs
    json.dump(allrecs, open(os.path.join(TMP, "corpus_sample.json"), "w"), indent=1)
    print(f"\nwrote corpus_sample.json: {len(allrecs)} tickets "
          f"({sum(1 for r in allrecs if r['repo']=='rebar')} rebar + "
          f"{sum(1 for r in allrecs if r['repo']=='snap')} snap)")
