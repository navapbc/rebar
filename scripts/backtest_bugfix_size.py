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

`--repeat-fix` additionally evaluates the SHIPPED repeat-fix predicate (ticket 1dd5) per
commit — again by importing it, so the backtest cannot drift from the gate — as a field
BESIDE `flagged`; `--labels-from-caused-by` labels each fix from the store's own `caused_by`
links ("this fix's ticket was later named as the cause of another bug") and prints labelled
recall + escalation rate per predicate, which is how the two signals are compared.

Usage:  python scripts/backtest_bugfix_size.py [--rev-range origin/main] [--out artifact.json]
        python scripts/backtest_bugfix_size.py --check-planning-corpus [--out artifact.json]
        python scripts/backtest_bugfix_size.py --repeat-fix --labels-from-caused-by
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar import config as _config  # noqa: E402
from rebar._commands.verify_commit import extract_ticket_refs  # noqa: E402
from rebar._engine_support.resolver import resolve_ticket_id  # noqa: E402
from rebar.llm.code_review.bugfix_size_gate import (  # noqa: E402
    BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES,
    count_non_test_diff_lines,
    non_test_paths_in_diff,
)
from rebar.llm.code_review.repeat_fix import (  # noqa: E402
    REPEAT_FIX_MIN_PRIOR,
    REPEAT_FIX_WINDOW_DAYS,
    repeat_fix_escalates,
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


# raw-git-ok: read-oriented git helper, variable subcommand
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


def _measure(sha: str, ticket: str, *, repeat_fix: bool = False) -> dict:
    diff = _git("show", sha, "--format=")
    non_test = count_non_test_diff_lines(diff)
    row = {
        "commit": sha,
        "ticket": ticket,
        "non_test_lines": non_test,
        # `flagged` means the SIZE FLOOR alone, for every consumer including
        # --check-planning-corpus. The repeat-fix verdict sits BESIDE it, never inside it.
        "flagged": non_test > BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES,
    }
    if repeat_fix:
        hit, priors = repeat_fix_escalates(
            non_test_paths_in_diff(diff),
            repo_root=str(REPO_ROOT),
            base_ref=f"{sha}~1",
            at=_commit_time(sha),
        )
        row["repeat_fix"] = hit
        row["repeat_fix_priors"] = priors
    return row


def _commit_time(sha: str) -> float | None:
    """The commit's own timestamp — the window anchor, so the backtest asks what the gate
    would have seen AT REVIEW TIME rather than what history looks like today."""
    try:
        return float(_git("log", "-1", "--format=%ct", sha).strip())
    except Exception:  # noqa: BLE001 — unreadable timestamp → predicate falls back to now
        return None


def _caused_by_culprits() -> set[str]:
    """Tickets some bug's ``caused_by`` names — the store's own "this fix later introduced a
    bug" label, aggregated exactly as ``rebar metrics`` aggregates it."""
    from rebar.metrics.bug_trends import caused_by_fan_in

    return set(caused_by_fan_in(str(REPO_ROOT)) or {})


def _recall_summary(rows: list[dict], culprits: set[str], repeat_fix: bool) -> dict:
    """Per-predicate labelled recall and escalation rate over `rows`."""
    total = len(rows)
    labelled = [r for r in rows if r["ticket"] in culprits]
    predicates = {"size": lambda r: bool(r["flagged"])}
    if repeat_fix:
        predicates["repeat-fix"] = lambda r: bool(r.get("repeat_fix"))
        predicates["union"] = lambda r: bool(r["flagged"]) or bool(r.get("repeat_fix"))
    per_predicate: dict[str, dict[str, Any]] = {}
    for name, fires in predicates.items():
        escalated = [r for r in rows if fires(r)]
        caught = [r for r in labelled if fires(r)]
        per_predicate[name] = {
            "escalated": len(escalated),
            "escalation_rate": round(len(escalated) / total, 4) if total else 0.0,
            "caught": len(caught),
            "recall": round(len(caught) / len(labelled), 4) if labelled else 0.0,
        }
    return {"total": total, "labelled": len(labelled), "predicates": per_predicate}


def _print_recall(summary: dict) -> None:
    print(
        f"labelled (fix whose ticket some bug names as caused_by): "
        f"{summary['labelled']} of {summary['total']}"
    )
    for name, stats in summary["predicates"].items():
        print(
            f"  {name:<11} recall {stats['caught']}/{summary['labelled']} "
            f"({stats['recall'] * 100:.1f}%)   escalation "
            f"{stats['escalated']}/{summary['total']} ({stats['escalation_rate'] * 100:.1f}%)"
        )


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
    parser.add_argument(
        "--repeat-fix",
        action="store_true",
        help=f"also evaluate the SHIPPED repeat-fix predicate per commit (a non-test file "
        f"touched by >={REPEAT_FIX_MIN_PRIOR} other bug-fix commits in the prior "
        f"{REPEAT_FIX_WINDOW_DAYS} days), as a field beside `flagged`",
    )
    parser.add_argument(
        "--labels-from-caused-by",
        action="store_true",
        help="label each fix from the store's caused_by links and report labelled recall "
        "plus escalation rate per predicate",
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
                rows.append(_measure(sha, ticket, repeat_fix=args.repeat_fix))
    else:
        shas = _git("log", "--no-merges", "--format=%H", args.rev_range).split()
        for sha in shas:
            message = _git("log", "-1", "--format=%B", sha)
            ticket = _resolve_bug_ticket(extract_ticket_refs(message) or [], tracker, type_cache)
            if ticket:
                rows.append(_measure(sha, ticket, repeat_fix=args.repeat_fix))

    flagged = [r for r in rows if r["flagged"]]
    artifact: dict[str, Any] = {
        "threshold": BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES,
        "rev_range": PLANNING_CORPUS_BOUNDARY if args.check_planning_corpus else args.rev_range,
        "corpus_rule": (
            "planning:subject-prefix" if args.check_planning_corpus else "gate:trailer-or-subject"
        ),
        "total_bugfix_commits": len(rows),
        "flagged_count": len(flagged),
        "commits": rows,
    }
    if args.repeat_fix:
        artifact["repeat_fix"] = {
            "window_days": REPEAT_FIX_WINDOW_DAYS,
            "min_prior": REPEAT_FIX_MIN_PRIOR,
            "escalated_count": sum(1 for r in rows if r.get("repeat_fix")),
        }
    recall: dict = {}
    if args.labels_from_caused_by:
        recall = _recall_summary(rows, _caused_by_culprits(), args.repeat_fix)
        artifact["recall"] = recall
    Path(args.out).write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(
        f"bug-fix commits: {len(rows)}; flagged (> {BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES} "
        f"non-test lines): {len(flagged)} -> {args.out}"
    )
    for r in flagged:
        print(f"  {r['commit'][:12]}  {r['non_test_lines']:>5}  {r['ticket']}")
    if args.labels_from_caused_by:
        _print_recall(recall)

    if args.check_planning_corpus:
        flagged_set = {r["commit"] for r in flagged}
        ok = len(rows) == PLANNING_CORPUS_SIZE and flagged_set == set(ADJUDICATED_SUBSTANTIVE)
        missed = set(ADJUDICATED_SUBSTANTIVE) - flagged_set
        mechanical_fp = flagged_set - set(ADJUDICATED_SUBSTANTIVE)
        print(
            f"planning-corpus check: corpus={len(rows)} (want {PLANNING_CORPUS_SIZE}); "
            f"missed={sorted(s[:12] for s in missed)}; "
            f"mechanical_fp={sorted(s[:12] for s in mechanical_fp)}; "
            f"{'PASS' if ok else 'FAIL'}"
        )
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
