#!/usr/bin/env python3
"""One-shot backtest for the Gerrit bugfix-size attestation criterion (ticket ad0d B2).

Re-derives the historical bug-fix corpus from git history and applies the SHARED size floor
(`BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES`) + non-test classification rule from
`rebar.llm.code_review.bugfix_size_gate` — the exact code the review bot runs — so the
backtest cannot drift from the gate. Emits a JSON artifact:

    {"threshold": 150, "total_bugfix_commits": N, "flagged_count": M,
     "commits": [{"commit", "ticket", "non_test_lines", "flagged"}, ...]}

Adjudication basis (planning corpus, recorded on ticket ad0d-6338-bc8c-43b6): 13 of 113
bug-fix commits exceeded the floor; ALL 13 were adjudicated substantive (two shipped ADRs).
A mechanical false positive means exactly: a flagged commit outside that adjudicated-13 set.

The planning corpus is pinned and reproducible: the 113 commits are the ancestors of
PLANNING_CORPUS_BOUNDARY whose SUBJECT line carries a `<ticket-id>:` prefix resolving to a
bug ticket — the measurement rule the planning analysis used. (The default mode resolves
trailers too, matching the shipped gate; over the same range that wider rule sees 245
bug-fix commits, whose extra flagged members are outside the hand-adjudicated corpus.)
`--check-planning-corpus` re-derives the corpus and exits non-zero unless the flagged set
equals ADJUDICATED_SUBSTANTIVE exactly — the executable form of the acceptance criterion.

Usage:  python scripts/backtest_bugfix_size.py [--rev-range origin/main] [--out artifact.json]
        python scripts/backtest_bugfix_size.py --check-planning-corpus [--out artifact.json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar import config as _config  # noqa: E402
from rebar._commands.verify_commit import extract_ticket_refs  # noqa: E402
from rebar._engine_support.resolver import resolve_ticket_id  # noqa: E402
from rebar.llm.code_review.bugfix_size_gate import (  # noqa: E402
    BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES,
    count_non_test_diff_lines,
)

# Newest commit of the 113-commit planning corpus (subject-prefix bug-fix commits).
PLANNING_CORPUS_BOUNDARY = "25fa6107f36f69b4b3731a4aaa9f5622aaec2988"
PLANNING_CORPUS_SIZE = 113

# The 13 commits hand-adjudicated substantive in the planning corpus (all >150 non-test
# lines; two shipped ADRs; zero mechanical cases). The gate rule must flag exactly these.
ADJUDICATED_SUBSTANTIVE = frozenset(
    {
        "3aec0ae90abb49acce2df6ee99d3956de0052f4f",
        "08b47eb78e144dec552468aa5bf805e4f8e444e6",
        "5ef138d26e815d9b583a48656e3cb50e8fb02ab9",
        "96a0d5c3f2fa32f97c7bce1a472c499041ab3833",
        "262616961cb416ef1f430579a90607046ad5389e",
        "74051ade15c3a717c46280be2c914b7c6639b18c",
        "517e0470ab011b89e6ec9a0deaa6fae5db8a9327",
        "992b0656d40e0e39b37fdc21bd7057ae0dc877da",
        "0fdb9fc6cbeedde4156fc01bddac16b58d161e70",
        "d5733dbc97d7b12b73e5ce4bd1bbb29bc135d072",
        "27e1165cdbbc70e6f3ff5068631ce3c27755a6ee",
        "e6f3283a0d0a522fc38440f3e6b238478568704c",
        "485873652981bc793ce892a08d69c95b16bd2248",
    }
)

_SUBJECT_TICKET_PREFIX = re.compile(r"^([0-9a-f]{4}(?:-[0-9a-f]{4}){0,3}):")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=True
    ).stdout


def _ticket_type(state_path: Path) -> str | None:
    """The ticket's current type, read via the same reducer path `rebar show` uses."""
    from rebar import _reads

    try:
        state = _reads.show_ticket(state_path.name, repo_root=str(REPO_ROOT))
    except Exception:  # noqa: BLE001 — unresolvable ticket → not part of the corpus
        return None
    return str(state.get("ticket_type") or "") or None


def _resolve_bug_ticket(
    refs: list[str], tracker: str, type_cache: dict[str, str | None]
) -> str | None:
    """First ref resolving to a bug ticket, else None."""
    for ref in refs:
        resolved = resolve_ticket_id(ref, tracker)
        if not resolved:
            continue
        ticket = str(resolved)
        if ticket not in type_cache:
            type_cache[ticket] = _ticket_type(Path(tracker) / ticket)
        return ticket if type_cache[ticket] == "bug" else None
    return None


def _measure(sha: str, ticket: str) -> dict:
    diff = _git("show", sha, "--format=")
    non_test = count_non_test_diff_lines(diff)
    return {
        "commit": sha,
        "ticket": ticket,
        "non_test_lines": non_test,
        "flagged": non_test > BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rev-range", default="origin/main", help="rev to walk (default: origin/main)"
    )
    parser.add_argument("--out", default="backtest_bugfix_size.json", help="artifact path")
    parser.add_argument(
        "--check-planning-corpus",
        action="store_true",
        help="re-derive the pinned 113-commit planning corpus and assert the flagged set "
        "equals the hand-adjudicated 13 exactly (exit 1 on any mismatch)",
    )
    args = parser.parse_args()

    tracker = str(_config.tracker_dir(str(REPO_ROOT)))
    type_cache: dict[str, str | None] = {}
    rows: list[dict] = []

    if args.check_planning_corpus:
        log = _git("log", "--no-merges", "--format=%H\t%s", PLANNING_CORPUS_BOUNDARY)
        for line in log.splitlines():
            sha, subject = line.split("\t", 1)
            match = _SUBJECT_TICKET_PREFIX.match(subject)
            if not match:
                continue
            ticket = _resolve_bug_ticket([match.group(1)], tracker, type_cache)
            if not ticket:
                # Ambiguous short prefix (resolver refuses) — the trailer carries the full id.
                message = _git("log", "-1", "--format=%B", sha)
                prefix = match.group(1)
                refs = [r for r in (extract_ticket_refs(message) or []) if r.startswith(prefix)]
                ticket = _resolve_bug_ticket(refs, tracker, type_cache)
            if ticket:
                rows.append(_measure(sha, ticket))
    else:
        shas = _git("log", "--no-merges", "--format=%H", args.rev_range).split()
        for sha in shas:
            message = _git("log", "-1", "--format=%B", sha)
            ticket = _resolve_bug_ticket(extract_ticket_refs(message) or [], tracker, type_cache)
            if ticket:
                rows.append(_measure(sha, ticket))

    flagged = [r for r in rows if r["flagged"]]
    artifact = {
        "threshold": BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES,
        "rev_range": PLANNING_CORPUS_BOUNDARY if args.check_planning_corpus else args.rev_range,
        "corpus_rule": (
            "planning:subject-prefix" if args.check_planning_corpus else "gate:trailer-or-subject"
        ),
        "total_bugfix_commits": len(rows),
        "flagged_count": len(flagged),
        "commits": rows,
    }
    Path(args.out).write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(  # noqa: T201 — one-shot analysis CLI; the report IS its operational stdout
        f"bug-fix commits: {len(rows)}; flagged (> {BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES} "
        f"non-test lines): {len(flagged)} -> {args.out}"
    )
    for r in flagged:
        print(f"  {r['commit'][:12]}  {r['non_test_lines']:>5}  {r['ticket']}")  # noqa: T201 — report body

    if args.check_planning_corpus:
        flagged_set = {r["commit"] for r in flagged}
        ok = len(rows) == PLANNING_CORPUS_SIZE and flagged_set == set(ADJUDICATED_SUBSTANTIVE)
        missed = set(ADJUDICATED_SUBSTANTIVE) - flagged_set
        mechanical_fp = flagged_set - set(ADJUDICATED_SUBSTANTIVE)
        print(  # noqa: T201 — check-mode verdict line, the script's contract output
            f"planning-corpus check: corpus={len(rows)} (want {PLANNING_CORPUS_SIZE}); "
            f"missed={sorted(s[:12] for s in missed)}; "
            f"mechanical_fp={sorted(s[:12] for s in mechanical_fp)}; "
            f"{'PASS' if ok else 'FAIL'}"
        )
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
