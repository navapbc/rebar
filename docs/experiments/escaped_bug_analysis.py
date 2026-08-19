#!/usr/bin/env python3
"""Escaped-bug analysis: did a non-blocking code-review finding already flag each regression?

Population: bug tickets closed with close_class=='regression' (a defect NEWLY introduced by a
change -> the classic "escaped code review" case; preexisting/plan_defect/env_integration/flaky/
duplicate/not_a_bug are excluded as non-escapes).

For each regression bug we know the buggy files (fix file_impact) and the file it was filed at
(created_at). We index every code-review sidecar by reviewed file (union of `deps` keys +
per-finding location paths), with verdict + timestamp + the non-blocking findings on that file.

For each regression bug, for each buggy SOURCE file, we collect PASS reviews of that file BEFORE
the bug was filed and any co-located NON-BLOCKING finding (advisory/coaching/dropped). Output:
  * (a) bugs with a co-located non-blocking finding  -> a tighter threshold/posture MIGHT have caught it
  * (b) bugs with NO co-located finding             -> a criteria gap (nothing looked there)
The (a) set requires human adjudication (does the finding actually describe the defect?); this
script surfaces the candidate findings verbatim for that read.
"""
from __future__ import annotations
import json, glob, os, collections, re

TRACKER = ".tickets-tracker"
NONBLOCK_POOLS = ("advisory", "coaching", "dropped")


def load_bugs(classes: set[str]) -> list[dict]:
    bugs = []
    for cache in glob.glob(f"{TRACKER}/*/.cache.json"):
        try:
            st = json.load(open(cache))["state"]
        except Exception:
            continue
        if st.get("ticket_type") != "bug" or st.get("close_class") not in classes:
            continue
        fi = st.get("file_impact") or []
        paths = [f.get("path") for f in fi if isinstance(f, dict) and f.get("path")]
        text = (st.get("description") or "") + " " + " ".join(
            c.get("body", "") for c in (st.get("comments") or [])
        )
        # also mine cited src/tests paths from prose (belt-and-suspenders vs missing file_impact)
        cited = set(re.findall(r"(?:src|tests|docs|infra|scripts)/[\w./-]+\.\w+", text))
        bugs.append({
            "alias": st.get("alias"), "id": st.get("ticket_id"),
            "created_at": st.get("created_at") or 0, "title": st.get("title") or "",
            "close_class": st.get("close_class"),
            "impact_paths": paths, "cited_paths": sorted(cited),
            "desc": st.get("description") or "",
        })
    return bugs


def review_location_path(f: dict) -> str | None:
    loc = str(f.get("location") or "").strip()
    return loc.split(":", 1)[0].strip() or None if loc else None


_STOP = {"the", "and", "for", "that", "this", "with", "not", "but", "are", "was", "its", "into",
         "test", "tests", "src", "rebar", "self", "none", "true", "false", "def", "class", "from",
         "when", "then", "which", "would", "does", "has", "have", "new", "old", "via", "per"}


def idents(text: str) -> set[str]:
    """Distinctive code identifiers in prose: snake_case, CamelCase, --flags, dotted.attrs, quoted."""
    toks = set()
    for m in re.findall(r"`([^`]+)`", text):  # backtick code spans
        toks.update(re.findall(r"[A-Za-z_][\w.\-]{2,}", m))
    toks.update(re.findall(r"--[a-z][\w-]+", text))          # cli flags
    toks.update(re.findall(r"\b[a-z]+_[a-z_]{2,}\b", text))  # snake_case
    toks.update(re.findall(r"\b[a-z]+[A-Z]\w+\b", text))     # camelCase
    toks.update(re.findall(r"\b\w+\.(?:py|yaml|json|toml|md|sh|tf)\b", text))  # filenames
    return {t.lower() for t in toks if t.lower() not in _STOP and len(t) > 3}


def relevance(bug_text: str, finding_text: str) -> tuple[int, list[str]]:
    a, b = idents(bug_text), idents(finding_text)
    shared = sorted(a & b)
    return len(shared), shared


def build_file_index() -> dict[str, list[dict]]:
    """file path -> list of {ts, verdict, nonblock:[{pool,location,criteria,finding}], blocked_here:bool}."""
    idx: dict[str, list[dict]] = collections.defaultdict(list)
    for fp in glob.glob(f"{TRACKER}/**/*-REVIEW_RESULT.json", recursive=True):
        try:
            ev = json.load(open(fp))
        except Exception:
            continue
        d = ev.get("data") if isinstance(ev, dict) else None
        if not isinstance(d, dict) or not str(d.get("schema", "")).startswith("code_review_result"):
            continue
        ts = int(os.path.basename(fp).split("-")[0]) if os.path.basename(fp).split("-")[0].isdigit() else 0
        verdict = d.get("verdict")
        reviewed = set((d.get("deps") or {}).keys())
        per_file_nb: dict[str, list[dict]] = collections.defaultdict(list)
        per_file_block: set[str] = set()
        for pool in ("blocking", "advisory", "coaching", "dropped", "indeterminate"):
            for f in (d.get(pool) or []):
                if not isinstance(f, dict):
                    continue
                p = review_location_path(f)
                if p:
                    reviewed.add(p)
                if pool == "blocking" and p:
                    per_file_block.add(p)
                if pool in NONBLOCK_POOLS and p:
                    per_file_nb[p].append({
                        "pool": pool, "location": f.get("location"),
                        "criteria": f.get("criteria"),
                        "finding": (f.get("finding") or "")[:400],
                        "priority": f.get("priority"), "validity": f.get("validity"),
                    })
        for p in reviewed:
            idx[p].append({
                "ts": ts, "verdict": verdict, "change_id": d.get("change_id"),
                "nonblock": per_file_nb.get(p, []), "blocked_here": p in per_file_block,
            })
    return idx


def main() -> None:
    bugs = load_bugs({"regression"})
    idx = build_file_index()
    print(f"regression bugs: {len(bugs)} | files indexed from reviews: {len(idx)}\n")

    MATCH_BAR = 3  # >=3 shared distinctive identifiers => the finding plausibly describes the defect
    genuine, file_only, without_nb, no_prior_review = [], [], [], []
    for bug in bugs:
        src = [p for p in bug["impact_paths"] if p.startswith("src/")]
        cand_paths = [p for p in (src or bug["impact_paths"] or bug["cited_paths"]) if p]
        bug_text = bug["title"] + " " + bug["desc"]
        prior_reviews, scored = [], []
        for p in cand_paths:
            for rv in idx.get(p, []):
                if rv["ts"] < bug["created_at"] and rv["verdict"] == "PASS":
                    prior_reviews.append((p, rv))
                    for nb in rv["nonblock"]:
                        sc, shared = relevance(bug_text, nb.get("finding") or "")
                        scored.append((sc, shared, p, rv, nb))
        scored.sort(key=lambda x: -x[0])
        best = scored[0][0] if scored else 0
        rec = {**bug, "cand_paths": cand_paths, "n_prior_reviews": len(prior_reviews),
               "best_score": best, "top": scored[:6]}
        if best >= MATCH_BAR:
            genuine.append(rec)
        elif scored:
            file_only.append(rec)
        elif prior_reviews:
            without_nb.append(rec)
        else:
            no_prior_review.append(rec)

    print(f"(a) GENUINE: a non-blocking finding plausibly describes the SAME defect (>= {MATCH_BAR} shared idents): {len(genuine)}")
    print(f"(a-weak) file had non-blocking findings but none defect-matched (< {MATCH_BAR}):                        {len(file_only)}")
    print(f"(b) file PASS-reviewed but ZERO non-blocking findings on it (criteria gap):                            {len(without_nb)}")
    print(f"( ) no prior PASS review of the buggy file in the corpus (not joinable):                               {len(no_prior_review)}\n")

    print("=" * 100)
    print(f"(a) GENUINE defect-matched non-blocking findings (adjudicate — would tighter posture catch it?):")
    print("=" * 100)
    for r in sorted(genuine, key=lambda x: -x["best_score"]):
        print(f"\n### {r['alias']}  score={r['best_score']}  {r['title'][:78]}")
        for sc, shared, p, rv, nb in r["top"]:
            if sc < MATCH_BAR:
                continue
            print(f"  [{nb['pool']}] {nb['location']} crit={nb['criteria']} prio={nb['priority']} val={nb['validity']}")
            print(f"    shared={shared}")
            print(f"    {(nb['finding'] or '')[:240]}")

    print("\n" + "=" * 100)
    print("(a-weak) file-level findings only, no defect match — titles for spot-check:")
    print("=" * 100)
    for r in sorted(file_only, key=lambda x: -x["best_score"]):
        print(f"  {r['alias']:<34} best={r['best_score']} {r['title'][:78]}")

    print("\n" + "=" * 100)
    print("(b) CRITERIA-GAP — file PASS-reviewed but NO non-blocking finding touched it:")
    print("=" * 100)
    for r in sorted(without_nb, key=lambda x: -x["n_prior_reviews"]):
        print(f"  {r['alias']:<34} reviews={r['n_prior_reviews']:<3} {r['title'][:78]}")

    print("\n" + "=" * 100)
    print("( ) NO-PRIOR-REVIEW — introducing change predates the corpus / not joinable:")
    print("=" * 100)
    for r in no_prior_review:
        print(f"  {r['alias']:<34} {r['title'][:84]}")


if __name__ == "__main__":
    main()
