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

The planning corpus is pinned by IDENTITY: PLANNING_CORPUS_COMMITS lists the exact 113
commit SHAs the planning analysis measured — the ancestors of PLANNING_CORPUS_BOUNDARY
whose SUBJECT line carried a `<ticket-id>:` prefix resolving to a bug ticket AS OF the
tracker state when ticket ad0d-6338-bc8c-43b6 was filed (tickets branch c5b7d41419e8).
Deriving membership from LIVE ticket state instead is what this check originally did, and
it drifts: reclassifying any historical ticket to/from `bug`, or adding a ticket that
changes short-prefix resolution, moves the count (bug 648b-7882-497f-45fc). The SHA pin is
immune to tracker churn. (The default mode resolves trailers too, matching the shipped
gate; over the same range that wider rule sees 245 bug-fix commits, whose extra flagged
members are outside the hand-adjudicated corpus.)
`--check-planning-corpus` measures the pinned commits and exits non-zero unless the
measured set equals the pin and the flagged set equals ADJUDICATED_SUBSTANTIVE exactly —
the executable form of the acceptance criterion. Per-row ticket labels are best-effort
against live state, for the artifact only; they no longer gate.

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
    escalation_for_diff,
)
from rebar.llm.code_review.repeat_fix import (  # noqa: E402
    REPEAT_FIX_MIN_PRIOR,
    REPEAT_FIX_WINDOW_DAYS,
)

# Newest commit of the 113-commit planning corpus (subject-prefix bug-fix commits).
PLANNING_CORPUS_BOUNDARY = "25fa6107f36f69b4b3731a4aaa9f5622aaec2988"

# The 113 planning-corpus commits, pinned by SHA. Recovered by re-running the
# subject-prefix derivation against the tracker state at ticket ad0d-6338-bc8c-43b6's
# creation (tickets branch c5b7d41419e877f47e6aa326ac5cac7aaff1d48c, via
# REBAR_TRACKER_DIR): corpus=113, flagged set == ADJUDICATED_SUBSTANTIVE exactly.
PLANNING_CORPUS_COMMITS = frozenset(
    {
        "25fa6107f36f69b4b3731a4aaa9f5622aaec2988",
        "9741b7b25409e0d54a32eb5a5de7f49c7b30c900",
        "cc6e234f8a89ff59f00368dfe2620c34a159038d",
        "3aec0ae90abb49acce2df6ee99d3956de0052f4f",
        "bf31290b2150948e2e32f13133e2cf6bb411fe11",
        "08b47eb78e144dec552468aa5bf805e4f8e444e6",
        "9fcf1cd3a2edd502d879db5d001e4ac42338e3ae",
        "df18817981eb97824b326a973ba5ba8aec4d9796",
        "136aa4f157afa01665659abb622dc191c0ad1b3e",
        "b1dd35483c8912d8b69069ffe36651349810313b",
        "dcd594cd6cd92ca58046a1e03fea55b73749f5aa",
        "c55c3dcfd45e37273beb58fff1178635843473f7",
        "e47ac676e4def79451f157563ea78f3855c975ca",
        "5ef138d26e815d9b583a48656e3cb50e8fb02ab9",
        "10d0542b1335fe5a2f82fcfef1c6376c867f9eeb",
        "6c22508c32a31ddc4850714b27a8e6a3483315d9",
        "082e967e5dcb9b6989000b9bac914afde1a77f1f",
        "6ba040420a511bc001811e5b0760708e521e84b6",
        "2d0827b84df43587eabbc0c612c3924e0cc57b0b",
        "ed9d5e28dc7942ba26dbf17cc23e670998cad37e",
        "96a0d5c3f2fa32f97c7bce1a472c499041ab3833",
        "fde6baae726f34ae00f008323426cc283833a1d9",
        "09be459337e1f872f689041caa5c61f9c1fd6757",
        "2a3abe6abde981fe575728b58043b6c06baa7136",
        "7368c9189a13732dac5d1993cd23a68d22772cfe",
        "132a0f9dcacfc2e8cbd525aea4c4809e9b45a1ae",
        "b93fd445018efdd961e5e3986d97c6b7546ee74d",
        "67c6055568abe204c125666e93a6307d33ca0471",
        "1a4e02bb8ab0ab70d71fb25c4a6178a6b2b00509",
        "b7e6ac96355e99f82ac4d333a8b19a0a7820a0dd",
        "262616961cb416ef1f430579a90607046ad5389e",
        "3e6bd26dd32973a32f2ed9e50ada0a886b4941d7",
        "57756496e1e6790ae857d63f66f5c22bbe1c526f",
        "3dfe8ba693a1ed7817f3b40810392bc37f65a383",
        "348598f8515ea553ede2f38fa9ce04398d1608d6",
        "f7aa0d5b598934997b831b2fda6897f028514608",
        "b992646735ee19b1c0aa86085a033b86aba0d2af",
        "bcd888352880929ace8a035263dacc054e286322",
        "74051ade15c3a717c46280be2c914b7c6639b18c",
        "032f30fdc83be76b7c6b6eef95ca4c415c928389",
        "61741d248c63e6d2125ad0639beaad7f3aa7500c",
        "7e3c50242da94ec2f1757b794c66ce434dc92f6a",
        "6526cfeae20392df0151b974d5d0f796535589eb",
        "517e0470ab011b89e6ec9a0deaa6fae5db8a9327",
        "457f99e29d401ed46901e5f2997bd062087731e3",
        "68bbbc9a1a22199f83491d269ba570b25cfadf99",
        "7c763f1a7e350485816fd020141c3acd9052ada0",
        "bc838aed27b6a03e93d80c46c74c27b532e45580",
        "f3dbe32b0b264854f635430e8589a7f98f201a4f",
        "992b0656d40e0e39b37fdc21bd7057ae0dc877da",
        "2281280d2ab6d5daeb6973c95815a3231010eda3",
        "1e7e5c9468f8679e3da982db809cf4c8add11379",
        "2e2fe3f7d25cd0d36b6bbc3bf07e3e923a6ddd31",
        "56a3ac16338a82db8f0ff7c9d7165de4eb786599",
        "f9fe49b0631f3189cce2d8948aa607f8eb35b1ea",
        "1837ba1b912c1280f1411160230b4e3dea1e5152",
        "426672159574812a70c116d9896b09641d3dfeec",
        "5e5c876f3c33faad3ff4b794db5d4057fb9ed4c9",
        "2437a68a4e18e4331f7c1b225f07b7041c571ef1",
        "e7d7f8067ccb8be9a24816c43181ee95176635f8",
        "2bf1fa924dcfcbf945c1ca09168c76d08c40423d",
        "55e2b5ccab481393fbc6bf3a8845fabcce016722",
        "fb7663157395e79e690a9ba5321a4bd5b151dc1b",
        "a4ff2801bebbde12574367d67248bc92f8e02165",
        "966f7cff332f9062389067127ab9e648279dfa52",
        "080f713e031795cfa1339044693e0b2a18377058",
        "7c3c08df88d0d477a44fa929d8fcd6b890351f3b",
        "62933ff621d8865ae92dbfe65eeb953b261f46ef",
        "8a1e4587e5806c13b574fb29ea1a7722677bc72a",
        "cb6f5362d462e26753d708a4413f54696734b9e3",
        "1475e2868de87e226bb8b70a5cb903335aad2e38",
        "b844490ab66244c4f98324e87ae00cfdf6504452",
        "d5fdbdc56405702427cb8428cb5eab65181d2bed",
        "450282688619b53d2710914bc9e865d8b7af2a12",
        "46064005ff3316d9e9ec1cdaf4b255353a9535c7",
        "87a86d19b0ba9248f8a631c688db1cff9615b011",
        "065a614f3094c8f6dd712e91f1748707930781b6",
        "dfe086bcbb937d87a3fbadc9774b8b31db5e7fe0",
        "bcc6ed0fa21eecb21b700c0c9d29568f58d15ca0",
        "8a26bcf5213595e7c3a82d6d2a28cc063214a1dd",
        "0fdb9fc6cbeedde4156fc01bddac16b58d161e70",
        "d5733dbc97d7b12b73e5ce4bd1bbb29bc135d072",
        "0f5dbc88b93ca741eff5935fba53cf268711853c",
        "5623a24960928a3797feaf31257f97e2fd068731",
        "3ed3662c915d4dd1e7be87f1d567c76fa31d3a6f",
        "ceda5f0e35674f7f0d1852c880c5e211697c3b0b",
        "aeb7f6224ebc466d1b1422114d7ea1ee274d0069",
        "3eb8001693848ddf0b554d535e79009125855acf",
        "661d2e03daffc7e8a01aa1d6380eae3bd667d474",
        "f2a8410a9da8207ba72035aacbc8af11730ecb8c",
        "d25a177c5c3ee7ec5ebd65fa570ea80ca50446ad",
        "7d77050364ebf96d446593ea7495ecf2c8f3146e",
        "429ea4e2626760748ebf0e052fc44dd34ce949a8",
        "82dcb2a189d89249c9fba3a50d17adf824c600fd",
        "a4364ab6412f096b74d44589ca0047486a5fc69f",
        "6942120def47daf91539cc2986078d0f56cce53f",
        "d7224cfecd6d37ece8450b869971d439882ceb29",
        "bd45d964073e62aad5efcd40c4cf10b8f921c9fa",
        "5f2d31a55e62c424fc5dcd820b0e315703b9d3d6",
        "cecb2591256c34f7b2523cb258f2d2c707dbff1d",
        "0209c5a27b19a9b1db1d1ade9c3014b7c78e0ecb",
        "5ce636f21910dc19db8f5127669148cd300b514a",
        "af433d2b4d8631f3e57d9825bfe206e2be87215c",
        "27e1165cdbbc70e6f3ff5068631ce3c27755a6ee",
        "98e95ab24fe0275d67bd4822488a124f68209c39",
        "a6cdad0e22b87523b97b95cb5209a7532c515980",
        "e6f3283a0d0a522fc38440f3e6b238478568704c",
        "207ac3c415c4da293fb8a1448c95233ede787662",
        "485873652981bc793ce892a08d69c95b16bd2248",
        "cb0f8ec3b0641860ea6139431ccc63ca528c4732",
        "43d1545237745f4666dcc13c81587c824251e51d",
        "6b25dbead186639a436b1d5faf7cb03e99001efc",
        "c4441f756c9a6bc38cfa3c76d859c8b26d10cdd0",
    }
)

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
        # Call the GATE, not the predicate underneath it. Re-deriving the escalation here
        # would make the backtest measure a second implementation of the rule and report the
        # number as if it were the gate's — and the two silently disagreed: the gate left the
        # window anchored at wall-clock `now` while this asked for the commit's own time, so
        # a replay of a year-old commit scored it against today's history. `escalation_for_diff`
        # now takes the anchor, so replay and review run the same code on the same window.
        _, reason, priors = escalation_for_diff(
            diff,
            repo_root=str(REPO_ROOT),
            base_ref=f"{sha}~1",
            at=_commit_time(sha),
        )
        row["repeat_fix"] = "repeat-fix" in reason
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


def _corpus_label(sha: str, subject: str, tracker: str) -> str:
    """Best-effort ticket label for a pinned planning-corpus row. Artifact metadata only —
    corpus membership is the SHA pin, so an unresolvable or reclassified ticket never
    changes the corpus; it just degrades this row's label to the raw subject prefix."""
    match = _SUBJECT_TICKET_PREFIX.match(subject)
    prefix = match.group(1) if match else ""
    message = _git("log", "-1", "--format=%B", sha)
    trailer_refs = [r for r in (extract_ticket_refs(message) or []) if r.startswith(prefix)]
    for ref in ([prefix] if prefix else []) + trailer_refs:
        resolved = resolve_ticket_id(ref, tracker)
        if resolved:
            return str(resolved)
    return prefix


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rev-range", default="origin/main", help="rev to walk (default: origin/main)"
    )
    parser.add_argument("--out", default="backtest_bugfix_size.json", help="artifact path")
    parser.add_argument(
        "--check-planning-corpus",
        action="store_true",
        help="measure the pinned 113-commit planning corpus and assert the flagged set "
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
            if sha not in PLANNING_CORPUS_COMMITS:
                continue
            label = _corpus_label(sha, subject, tracker)
            rows.append(_measure(sha, label, repeat_fix=args.repeat_fix))
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
            "planning:pinned-shas" if args.check_planning_corpus else "gate:trailer-or-subject"
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
        measured = {r["commit"] for r in rows}
        flagged_set = {r["commit"] for r in flagged}
        ok = measured == PLANNING_CORPUS_COMMITS and flagged_set == set(ADJUDICATED_SUBSTANTIVE)
        missed = set(ADJUDICATED_SUBSTANTIVE) - flagged_set
        mechanical_fp = flagged_set - set(ADJUDICATED_SUBSTANTIVE)
        unmeasured = PLANNING_CORPUS_COMMITS - measured
        print(
            f"planning-corpus check: corpus={len(rows)} "
            f"(pin {len(PLANNING_CORPUS_COMMITS)}); "
            f"unmeasured={sorted(s[:12] for s in unmeasured)}; "
            f"missed={sorted(s[:12] for s in missed)}; "
            f"mechanical_fp={sorted(s[:12] for s in mechanical_fp)}; "
            f"{'PASS' if ok else 'FAIL'}"
        )
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
