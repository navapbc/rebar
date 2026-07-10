#!/usr/bin/env python3
"""Run the d01e live-validation matrix and emit its JUnit + JSON reports (ADR 0037 §4).

One command makes the harness's report contract true end-to-end: it runs
``tests/integration/test_reconcile_live_e2e.py -m live`` with ``--junitxml`` pointed at
``reports/d01e-live-matrix.junit.xml``, then derives the JSON summary the ADR promises at
``reports/d01e-live-matrix.report.json`` (totals + one entry per test with outcome and
duration). Both are gitignored run artifacts; the durable evidence lives on ticket d01e.

The deterministic core runs green offline; the ``@_requires_live`` probes self-skip
without JIRA_URL / JIRA_USER / JIRA_API_TOKEN + acli. Extra pytest args pass through
after ``--`` (e.g. ``scripts/run_live_matrix.py -- -k c4 -q``). Exits with pytest's
return code (or 2 if the JUnit XML was never produced).
"""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MATRIX = "tests/integration/test_reconcile_live_e2e.py"
REPORTS_DIR = REPO_ROOT / "reports"
JUNIT_PATH = REPORTS_DIR / "d01e-live-matrix.junit.xml"
JSON_PATH = REPORTS_DIR / "d01e-live-matrix.report.json"


def _outcome(case: ET.Element) -> tuple[str, str | None]:
    for tag, outcome in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
        child = case.find(tag)
        if child is not None:
            return outcome, child.get("message")
    return "passed", None


def derive_json_summary(junit_path: Path, json_path: Path) -> dict[str, object]:
    root = ET.parse(junit_path).getroot()
    cases = root.iter("testcase")
    tests = []
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    duration = 0.0
    for case in cases:
        outcome, message = _outcome(case)
        counts[outcome] += 1
        duration += float(case.get("time") or 0.0)
        entry: dict[str, object] = {
            "name": case.get("name"),
            "classname": case.get("classname"),
            "outcome": outcome,
            "time_s": float(case.get("time") or 0.0),
        }
        if message:
            entry["message"] = message
        tests.append(entry)
    summary: dict[str, object] = {
        "matrix": MATRIX,
        "junit": str(junit_path.relative_to(REPO_ROOT)),
        "total": sum(counts.values()),
        **counts,
        "duration_s": round(duration, 3),
        "all_green": counts["failed"] == 0 and counts["error"] == 0,
        "tests": tests,
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main(argv: list[str]) -> int:
    extra = argv[1:]
    if extra and extra[0] == "--":
        extra = extra[1:]
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        MATRIX,
        "-m",
        "live",
        f"--junitxml={JUNIT_PATH}",
        *extra,
    ]
    rc = subprocess.run(cmd, cwd=REPO_ROOT, check=False).returncode
    if not JUNIT_PATH.exists():
        print(  # noqa: T201 — operator script; stderr diagnostic is its output surface
            f"error: pytest produced no JUnit XML at {JUNIT_PATH}", file=sys.stderr
        )
        return 2
    summary = derive_json_summary(JUNIT_PATH, JSON_PATH)
    print(  # noqa: T201 — operator script; the summary line is its stdout contract
        f"live matrix: {summary['passed']} passed, {summary['failed']} failed, "
        f"{summary['error']} error, {summary['skipped']} skipped "
        f"-> {JSON_PATH.relative_to(REPO_ROOT)}"
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
