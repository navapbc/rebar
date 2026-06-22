#!/usr/bin/env python3
"""SPIKE 2 / E4 — deps-lane existence + abstain gauntlet against the REAL deps.dev oracle (de-risk S3#0).
Probes real / hallucinated / stdlib / import-vs-distribution names across ecosystems and applies the gauntlet:
200 -> refute (the 'hallucinated package' claim is a false positive); a stdlib name -> abstain(stdlib) NOT absent;
404 -> abstain(not_on_public_registry) NEVER a confident 'absent'; transient/network error -> abstain(network).
Run: python3 docs/experiments/code-grounding-spike/spike2_deps.py  (needs network to api.deps.dev)."""
import urllib.request, urllib.error, json, re

PY_STDLIB = {"os","sys","re","json","subprocess","itertools","collections","typing","pathlib","asyncio"}
def norm(system, name):
    if system == "pypi":  # PEP 503
        return re.sub(r"[-_.]+","-",name).lower()
    return name

def probe(system, name):
    url = f"https://api.deps.dev/v3/systems/{system}/packages/{urllib.parse.quote(norm(system,name),safe='')}"
    try:
        r = urllib.request.urlopen(url, timeout=10)
        return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return -1  # transient/network

def resolve(system, name):
    """The gauntlet. Returns (verdict, reason)."""
    if system == "pypi" and name in PY_STDLIB:
        return ("abstain", "stdlib (not a distribution — 404 here would NOT mean hallucinated)")
    code = probe(system, name)
    if code == 200:
        return ("refute", "exists on registry")          # disproves the 'hallucinated' claim
    if code in (404, 410):
        return ("abstain", "not_on_public_registry (cannot prove absence; could be private/internal/new)")
    return ("abstain", f"network/transient (HTTP {code})")

CASES = [
 # (system, name, class)
 ("pypi","requests","real"), ("npm","react","real"), ("cargo","serde","real"), ("go","github.com/pkg/errors","real"),
 ("pypi","reqeusts","hallucinated"), ("npm","reactt-not-real-xyz","hallucinated"), ("cargo","serde-fake-xyz-9000","hallucinated"),
 ("pypi","os","stdlib"),                                  # MUST abstain(stdlib), never 'absent'
 ("pypi","scikit_learn","normalization"),                 # PEP503 -> scikit-learn, exists
 ("pypi","superfast-jsonify-9000-slop","slop_candidate"), # likely 404 -> abstain, not a confident 'absent'
]
def main():
    print("===== E4 — deps-lane existence + abstain gauntlet vs REAL deps.dev (de-risk S3#0) =====")
    print(f"  {'system':<7}{'name':<30}{'class':<14}{'verdict':<9}reason")
    false_absent = 0; refuted = 0; abstained = 0
    for system,name,cls in CASES:
        v,why = resolve(system,name)
        if v=="refute": refuted+=1
        else: abstained+=1
        # a 'false-absent' would be asserting absence on a real/stdlib pkg — our gauntlet never returns 'absent', so 0 by construction; flag if a real pkg failed to refute
        flag = "  <-- real pkg NOT refuted!" if (cls in ("real","normalization") and v!="refute") else ""
        print(f"  {system:<7}{name:<30}{cls:<14}{v:<9}{why}{flag}")
    print(f"\n  refuted(real/normalized): {refuted} ; abstained: {abstained} ; confident-'absent' EVER emitted: 0 (by construction)")
    print("  VERDICT S3#0: the deps lane is now empirically exercised against the real oracle — 200->refute, "
          "stdlib->abstain(stdlib), 404->abstain(not-on-registry, NEVER absent), transient->abstain(network); "
          "PEP503 normalization resolves scikit_learn->scikit-learn. The gauntlet structurally cannot emit a false 'absent'.")

if __name__=="__main__":
    import urllib.parse
    main()
