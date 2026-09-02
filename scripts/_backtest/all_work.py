"""All-work change-shape corpus: commit + ticket distributions vs offline rework signals.

This is the reproducible form of `reports/stability/change-shape-backtest.md` read (a).
It walks a rev range once, measures every commit's shape, folds commits into tickets by
their ``rebar-ticket:`` trailer, and correlates shape against the two rework signals that
need no network and no CI provider:

* ``caused_by`` fan-in — the store's own "this ticket was later named as a bug's cause".
* gate rounds — how many ``REVIEW_RESULT`` / ``COMPLETION_VERDICT`` events a ticket
  accumulated, i.e. how many times a gate had to be run.

The Gerrit ``Verified-1`` half of the report is deliberately NOT here: it needs
authenticated Gerrit access, and a measurement whose only trigger is one hosted service is
not portable. The report marks those figures as requiring credentials.

`non_test_lines` comes from the SHIPPED `count_non_test_diff_lines`, and test/non-test file
classification from the SHIPPED `is_test_path`, both imported from
`rebar.llm.code_review.bugfix_size_gate`. Neither is re-derived here: a re-derivation inside
this backtest has already silently disagreed with the gate once.
"""

from __future__ import annotations

import subprocess
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rebar._commands.verify_commit import extract_ticket_refs
from rebar.llm.code_review.bugfix_size_gate import (
    BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES,
    count_non_test_diff_lines,
    is_test_path,
)

from . import stats

# Report window: `rebar-ticket` trailer coverage on origin/main is 60/650 in June but
# 876/907 in July, so June is pre-enforcement and its trailerless commits cannot be joined
# to tickets. `--until` is EXCLUSIVE, so the report's "2026-07-01 .. 2026-09-01" window is
# expressed as an until of 2026-09-02 to include the whole of 2026-09-01.
DEFAULT_SINCE = "2026-07-01"
DEFAULT_UNTIL = "2026-09-02"

_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"


def parse_day(value: str) -> float:
    """A ``YYYY-MM-DD`` boundary as a UTC epoch second."""
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()


# ---------------------------------------------------------------- commit corpus


def _diff_paths(diff: str) -> set[str]:
    """Every path named by a `---`/`+++` header in the diff (renames count both sides)."""
    paths: set[str] = set()
    for line in diff.splitlines():
        if not (line.startswith("+++ ") or line.startswith("--- ")):
            continue
        target = line[4:].strip()
        if target == "/dev/null":
            continue
        if target[:2] in ("a/", "b/"):
            target = target[2:]
        if target:
            paths.add(target)
    return paths


def _total_diff_lines(diff: str) -> int:
    return sum(
        1
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def _measure_record(record: str) -> dict[str, Any] | None:
    parts = record.split(_FIELD_SEP)
    if len(parts) < 5:
        return None
    sha, committed_at, subject, body = parts[0].strip(), parts[1], parts[2], parts[3]
    diff = parts[4]
    paths = _diff_paths(diff)
    return {
        "sha": sha,
        "committed_at": int(committed_at),
        "subject": subject,
        "ticket_refs": extract_ticket_refs(body) or [],
        # Imported from the shipped gate — never re-derived here.
        "non_test_lines": count_non_test_diff_lines(diff),
        "total_lines": _total_diff_lines(diff),
        "files": sorted(paths),
        "files_non_test": sum(1 for path in paths if not is_test_path(path)),
    }


# raw-git-ok: read-oriented history walk, no writes
def collect_commits(repo_root: Path, rev_range: str) -> list[dict[str, Any]]:
    """One `git log --no-merges -p -U0` pass over ``rev_range``, measured per commit."""
    raw = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "log",
            "--no-merges",
            f"--format={_RECORD_SEP}%H{_FIELD_SEP}%ct{_FIELD_SEP}%s{_FIELD_SEP}%B{_FIELD_SEP}",
            "-p",
            "-U0",
            rev_range,
        ],
        capture_output=True,
        text=True,
        check=True,
        errors="replace",
    ).stdout
    rows: list[dict[str, Any]] = []
    for record in raw.split(_RECORD_SEP):
        if not record.strip():
            continue
        measured = _measure_record(record)
        if measured is not None:
            rows.append(measured)
    return rows


def in_window(rows: Iterable[dict[str, Any]], since: float, until: float) -> list[dict[str, Any]]:
    """``[since, until)`` on commit time — half-open, so a day boundary is unambiguous."""
    return [row for row in rows if since <= row["committed_at"] < until]


# ---------------------------------------------------------------- ticket corpus


def ticket_ids(tracker_dir: Path) -> set[str]:
    """Every ticket directory in the store.

    Not just the canonical four-quad ids: a Jira-bridged ticket's directory is named
    ``jira-<key>``, and commits do carry those as trailers, so filtering to the four-quad
    shape silently drops them from the corpus.
    """
    return {entry.name for entry in tracker_dir.iterdir() if entry.is_dir()}


def _prefix_index(full_ids: Iterable[str]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = defaultdict(list)
    for full_id in full_ids:
        for key in (full_id[:4], full_id[:9], full_id[:14], full_id):
            index[key].append(full_id)
    return index


def group_by_ticket(
    rows: Sequence[dict[str, Any]], full_ids: set[str]
) -> tuple[dict[str, dict[str, Any]], int]:
    """Fold commits into tickets by their first ``rebar-ticket:`` ref.

    Returns the ticket corpus and the number of commits whose ref did not resolve to a
    single full canonical id (ambiguous 4-quad fragments, or refs for tickets no longer in
    the store).
    """
    index = _prefix_index(full_ids)
    tickets: dict[str, dict[str, Any]] = {}
    unresolved = 0
    for row in rows:
        refs = row["ticket_refs"]
        candidates = set(index.get(refs[0], [])) if refs else set()
        if len(candidates) != 1:
            unresolved += 1
            continue
        ticket = tickets.setdefault(
            candidates.pop(),
            {"non_test_lines": 0, "total_lines": 0, "commits": 0, "file_set": set()},
        )
        ticket["non_test_lines"] += row["non_test_lines"]
        ticket["total_lines"] += row["total_lines"]
        ticket["commits"] += 1
        ticket["file_set"].update(row["files"])
    for ticket in tickets.values():
        ticket["files"] = len(ticket.pop("file_set"))
    return tickets, unresolved


# ---------------------------------------------------------------- rework signals


def gate_rounds(tracker_dir: Path) -> dict[str, dict[str, int]]:
    """Per-ticket gate-run counts, from the durable sidecar events in the tracker.

    A ``REVIEW_RESULT`` event is one plan-review run; a ``COMPLETION_VERDICT`` is one
    completion-verifier run. More rounds means the ticket needed the gate more than once.
    """
    rounds: dict[str, dict[str, int]] = {}
    for entry in sorted(tracker_dir.iterdir()):
        if not entry.is_dir():
            continue
        review = sum(1 for _ in entry.glob("*-REVIEW_RESULT.json"))
        completion = sum(1 for _ in entry.glob("*-COMPLETION_VERDICT.json"))
        if review or completion:
            rounds[entry.name] = {"review": review, "completion": completion}
    return rounds


def attach_signals(
    tickets: dict[str, dict[str, Any]],
    rounds: dict[str, dict[str, int]],
    fan_in: dict[str, int],
) -> None:
    for ticket_id, ticket in tickets.items():
        ticket["review_rounds"] = rounds.get(ticket_id, {}).get("review", 0)
        ticket["completion_rounds"] = rounds.get(ticket_id, {}).get("completion", 0)
        ticket["caused_by"] = int(fan_in.get(ticket_id, 0))


# ---------------------------------------------------------------- analysis


_SHAPE_KEYS = (("non_test_lines", "non-test LOC"), ("files", "files"), ("commits", "commits"))

_SIGNALS = (
    ("completion-gate rounds", "completion_rounds", True),
    ("plan-review rounds", "review_rounds", True),
    ("caused_by fan-in", "caused_by", False),
)


def correlations(tickets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Spearman of each shape variable against each rework signal.

    Gate-round signals are measured over the tickets that HAVE that signal (a ticket with
    zero recorded rounds never reached the gate, so it carries no information about how
    many rounds the gate needed); ``caused_by`` fan-in is measured over the whole corpus,
    where zero is a real observation ("never named as a cause").
    """
    rows = list(tickets.values())
    out: list[dict[str, Any]] = []
    for label, key, positives_only in _SIGNALS:
        subset = [t for t in rows if t[key] > 0] if positives_only else rows
        entry: dict[str, Any] = {"signal": label, "n": len(subset), "vs": {}}
        if len(subset) >= 10:
            ys = [float(t[key]) for t in subset]
            for shape_key, shape_label in _SHAPE_KEYS:
                xs = [float(t[shape_key]) for t in subset]
                entry["vs"][shape_label] = stats.correlate(xs, ys)
        out.append(entry)
    return out


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _group_summary(group: Sequence[dict[str, Any]]) -> dict[str, Any]:
    completion = [t["completion_rounds"] for t in group if t["completion_rounds"] > 0]
    review = [t["review_rounds"] for t in group if t["review_rounds"] > 0]
    caused = sum(1 for t in group if t["caused_by"] > 0)
    return {
        "n": len(group),
        "mean_completion_rounds": _mean(completion),
        "mean_plan_review_rounds": _mean(review),
        "caused_by_tickets": caused,
        "caused_by_rate": round(100 * caused / len(group), 1) if group else 0.0,
    }


def threshold_split(tickets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The above/below split at the SHIPPED threshold constant — never a literal."""
    rows = list(tickets.values())
    above = [t for t in rows if t["non_test_lines"] > BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES]
    below = [t for t in rows if t["non_test_lines"] <= BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES]
    return {
        "threshold": BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES,
        "above": _group_summary(above),
        "below": _group_summary(below),
    }


def build_report(
    *,
    repo_root: Path,
    tracker_dir: Path,
    rev_range: str,
    since: str,
    until: str,
) -> dict[str, Any]:
    """The whole all-work read, as one JSON-serialisable artefact."""
    from rebar.metrics.bug_trends import caused_by_fan_in

    commits = collect_commits(repo_root, rev_range)
    window = in_window(commits, parse_day(since), parse_day(until))
    tickets, unresolved = group_by_ticket(window, ticket_ids(tracker_dir))
    attach_signals(
        tickets,
        gate_rounds(tracker_dir),
        {str(k): int(v) for k, v in (caused_by_fan_in(str(repo_root)) or {}).items()},
    )
    ticket_rows = list(tickets.values())
    return {
        "mode": "all-work",
        "rev_range": rev_range,
        "window": {"since": since, "until": until, "until_exclusive": True},
        "threshold": BUGFIX_SIZE_THRESHOLD_NON_TEST_LINES,
        "commits_walked": len(commits),
        "commits_in_window": len(window),
        "commits_unresolved_ticket_ref": unresolved,
        "commit_distributions": {
            "non_test_lines": stats.describe([r["non_test_lines"] for r in window]),
            "total_diff_lines": stats.describe([r["total_lines"] for r in window]),
            "files_touched": stats.describe([len(r["files"]) for r in window]),
        },
        "tickets": len(tickets),
        "ticket_distributions": {
            "non_test_lines": stats.describe([t["non_test_lines"] for t in ticket_rows]),
            "files_union": stats.describe([t["files"] for t in ticket_rows]),
            "commits": stats.describe([t["commits"] for t in ticket_rows]),
        },
        "commits_per_ticket": dict(sorted(Counter(t["commits"] for t in ticket_rows).items())),
        "correlations": correlations(tickets),
        "threshold_split": threshold_split(tickets),
    }


# ---------------------------------------------------------------- rendering


def _render_distributions(title: str, block: dict[str, dict[str, float]]) -> list[str]:
    return [title] + [f"  {stats.format_describe(name, row)}" for name, row in block.items()]


def _render_correlations(entries: Sequence[dict[str, Any]]) -> list[str]:
    lines = ["", "Spearman rank correlation (ticket level):"]
    for entry in entries:
        lines.append(f"  {entry['signal']} (n={entry['n']})")
        if not entry["vs"]:
            lines.append("    too few tickets to correlate")
            continue
        for shape, cell in entry["vs"].items():
            lines.append(f"    vs {shape:<13} rho={cell['rho']:+.3f}  p={cell['p']:.2g}")
    return lines


def _render_split(split: dict[str, Any]) -> list[str]:
    lines = ["", f"At the shipped floor of {split['threshold']} non-test lines (ticket level):"]
    for name, key in ((f"> {split['threshold']}", "above"), (f"<= {split['threshold']}", "below")):
        group = split[key]
        lines.append(
            f"  {name:<7} n={group['n']:<6} "
            f"mean close-gate rounds={group['mean_completion_rounds']}  "
            f"mean plan-review rounds={group['mean_plan_review_rounds']}  "
            f"caused_by {group['caused_by_tickets']}/{group['n']} "
            f"({group['caused_by_rate']}%)"
        )
    return lines


def render(report: dict[str, Any]) -> str:
    window = report["window"]
    lines = [
        f"all-work change shape — {report['rev_range']}, --no-merges, "
        f"{window['since']} .. {window['until']} (until exclusive)",
        f"commits walked: {report['commits_walked']}; in window: {report['commits_in_window']}; "
        f"ticket-level corpus: {report['tickets']} "
        f"({report['commits_unresolved_ticket_ref']} commits had no resolvable ticket ref)",
        "",
    ]
    lines += _render_distributions("Commit level:", report["commit_distributions"])
    lines += [""]
    lines += _render_distributions("Ticket level:", report["ticket_distributions"])
    lines += ["", f"commits per ticket: {report['commits_per_ticket']}"]
    lines += _render_correlations(report["correlations"])
    lines += _render_split(report["threshold_split"])
    return "\n".join(lines)
