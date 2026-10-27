#!/usr/bin/env python3
"""SPIKE: T1-floor refutation yield via a repo-wide universal-ctags index, polyglot, cross-file.
Builds a polyglot fixture; each labelled reference is an 'absence claim' the oracle tries to REFUTE
(find the symbol's definition somewhere in the repo). Measures yield (real refs resolved), false-refute
(hallucinated refs wrongly 'found'), and abstain (correct: can't disprove)."""
import os, subprocess, json, shutil, collections

import shutil as _sh, tempfile as _tf
CTAGS = _sh.which("ctags") or "/opt/homebrew/bin/ctags"
ROOT = _tf.mkdtemp(prefix="cg-spike-")

FILES = {
 # --- Python: defs spread across files; api.py references core/util (cross-file) ---
 "pkg/core.py": "class TicketStore:\n    def __init__(self): self.events=[]\n\ndef reconcile_tickets(store): return store\n\ndef _merge_events(a,b): return a+b\n",
 "pkg/util.py": "def normalize_name(n): return n.strip().lower()\n",
 "pkg/api.py": "from .core import TicketStore, reconcile_tickets\nfrom .util import normalize_name\ndef handler(): return reconcile_tickets(TicketStore())\n",
 # --- JavaScript ---
 "web/store.js": "export function createTicket(t){return t}\nexport class EventLog{push(e){}}\n",
 "web/index.js": "import {createTicket, EventLog} from './store.js'\nconst l=new EventLog()\n",
 # --- TypeScript ---
 "ts/types.ts": "export interface ReconcileResult { ok: boolean }\nexport function parseConfig(s: string): ReconcileResult { return {ok:true} }\n",
 # --- Go ---
 "go/store/store.go": "package store\ntype Ticket struct { ID string }\nfunc NewStore() *Ticket { return &Ticket{} }\n",
 "go/cmd/main.go": "package main\nimport \"x/store\"\nfunc main(){ _ = store.NewStore() }\n",
 # --- Unparseable language (no ctags parser): a real symbol the floor CANNOT confirm -> must ABSTAIN ---
 "exotic/thing.qzx": "gadget QuantumThing { spin: up }\n",
}

# Labelled 'absence claims' (agent claims X doesn't exist). The oracle tries to refute by finding X.
# kind: real_crossfile = genuinely defined (should REFUTE) ; hallucinated = not defined (should ABSTAIN,
#        never false-refute) ; real_unparseable = defined only in an unsupported lang (should ABSTAIN = fail-open)
REFS = [
 ("TicketStore","py","real_crossfile"), ("reconcile_tickets","py","real_crossfile"),
 ("normalize_name","py","real_crossfile"), ("_merge_events","py","real_crossfile"),
 ("createTicket","js","real_crossfile"), ("EventLog","js","real_crossfile"),
 ("ReconcileResult","ts","real_crossfile"), ("parseConfig","ts","real_crossfile"),
 ("NewStore","go","real_crossfile"), ("Ticket","go","real_crossfile"),
 # hallucinated (typos / nonexistent)
 ("TicketStoer","py","hallucinated"), ("reconcile_all","py","hallucinated"),
 ("denormalize_name","py","hallucinated"), ("createTickets","js","hallucinated"),
 ("EventLogger","js","hallucinated"), ("ReconcileResults","ts","hallucinated"),
 ("parseConfigs","ts","hallucinated"), ("NewStorage","go","hallucinated"),
 ("Tickets","go","hallucinated"),
 # real but only in an unparseable language -> fail-open abstain
 ("QuantumThing","qzx","real_unparseable"),
]

def build():
    if os.path.exists(ROOT): shutil.rmtree(ROOT)
    for rel, content in FILES.items():
        p = os.path.join(ROOT, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(content)

def ctags_index():
    """Repo-wide ctags, JSON output. Returns {symbol_name: [(kind,file,lang)]} + set of langs parsed."""
    out = subprocess.run([CTAGS, "-R", "--output-format=json", "--fields=+lK", "-f", "-", ROOT],
                         capture_output=True, text=True)
    idx = collections.defaultdict(list); langs=set(); n=0
    for line in out.stdout.splitlines():
        try: t = json.loads(line)
        except Exception: continue
        if t.get("_type") != "tag": continue
        n += 1
        idx[t["name"]].append((t.get("kind"), os.path.relpath(t.get("path",""), ROOT), t.get("language")))
        if t.get("language"): langs.add(t["language"])
    return idx, langs, n

def main():
    build()
    idx, langs, ntags = ctags_index()
    print(f"ctags repo-wide index: {ntags} definitions across languages parsed: {sorted(langs)}")
    print(f"{'symbol':<20}{'lang':<6}{'label':<18}{'resolved?':<10}verdict")
    print("-"*72)
    rows=[]
    for name, lang, label in REFS:
        found = name in idx
        # oracle verdict: found -> REFUTE (disproved the absence claim); not found -> ABSTAIN (can't disprove)
        verdict = "REFUTE" if found else "abstain"
        rows.append((name,lang,label,found,verdict))
        where = (" @ "+idx[name][0][1]) if found else ""
        print(f"{name:<20}{lang:<6}{label:<18}{str(found):<10}{verdict}{where}")
    # metrics
    real=[r for r in rows if r[2]=="real_crossfile"]
    hall=[r for r in rows if r[2]=="hallucinated"]
    unp=[r for r in rows if r[2]=="real_unparseable"]
    yield_=sum(1 for r in real if r[3])/len(real)
    false_refute=sum(1 for r in hall if r[3])
    abstain_hall=sum(1 for r in hall if not r[3])
    print("\n=== METRICS ===")
    print(f"REFUTATION YIELD (real cross-file symbols resolved): {sum(1 for r in real if r[3])}/{len(real)} = {yield_*100:.0f}%")
    print(f"FALSE-REFUTE (hallucinated wrongly 'found'): {false_refute}/{len(hall)}  (MUST be 0)")
    print(f"ABSTAIN on hallucinated (correct, can't disprove): {abstain_hall}/{len(hall)}")
    print(f"FAIL-OPEN on unparseable lang (real symbol -> abstain, no false-refute): {'PASS' if not unp[0][3] else 'FAIL'}")
    # per-language yield
    bylang=collections.defaultdict(lambda:[0,0])
    for r in real:
        bylang[r[1]][1]+=1; bylang[r[1]][0]+=1 if r[3] else 0
    print("per-language yield:", {k:f'{v[0]}/{v[1]}' for k,v in sorted(bylang.items())})

if __name__=="__main__":
    main()
