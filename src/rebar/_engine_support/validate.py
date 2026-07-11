"""Repo-wide tracker-health check ``validate``.

Faithful in-process port of ``validate-issues.sh``: normalize every ticket from
``ticket list`` into the internal issue schema, run the nine health checks
(:mod:`rebar._engine_support.validate_checks`) in a fixed order, score the flat
finding stream, and render text / ``--terse`` / ``--output json`` exactly as bash
did — including the ANSI colors and the score-encoded exit (``exit == 5 - score``).

Data source: tickets are read in-process via ``list_states`` (the Tier C win,
byte-equivalent because the CLI's ``list`` arm is itself ``list_states``). The
ticket-command string (used only for the interface-contract *suggestion* text,
never subprocessed) is the in-process ``rebar`` CLI
(:func:`rebar._engine.in_process_cli`).

Output contract (docs/bash-migration.md §1.4): ``--output json`` is pinned by JSON
**schema + semantic** equality (jq vs ``json.dumps`` whitespace differs and is not
part of the contract); the human text / terse streams are byte-pinned, colors
included.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from rebar._engine_support import validate_checks as _checks
from rebar._engine_support.output import OutputFormatError, parse_output

# Colors (echo -e escapes in the bash original).
_RED = "\033[0;31m"
_YELLOW = "\033[1;33m"
_GREEN = "\033[0;32m"
_BLUE = "\033[0;34m"
_NC = "\033[0m"

# Per-severity text prefix + color (matches the bash log_* helpers).
_SEV_RENDER = {
    "critical": (_RED, "[CRITICAL]"),
    "major": (_RED, "[MAJOR]"),
    "minor": (_YELLOW, "[MINOR]"),
    "warning": (_YELLOW, "[WARNING]"),
    "verbose": (_BLUE, "[DEBUG]"),
    "suggestion": (_BLUE, "[SUGGESTION]"),
}
# Severities that print in text mode regardless of --verbose (the rest are gated).
_ALWAYS_SHOWN = {"critical", "major", "minor", "warning"}


def _default_ticket_cmd() -> str:
    from rebar._engine import in_process_cli

    return in_process_cli()


# ───────────────────────────── data + normalization ──────────────────────────
def _raw_tickets(tracker: str) -> list[dict]:
    """Raw ticket list, read in-process via ``list_states`` (the production path).

    Applies the uniform read-freshness policy here — the bash validate arm omitted
    init but got freshness transitively via its nested ``ticket list``; we mirror
    that without a subprocess."""
    from rebar._engine_support import reads

    reads.ensure_fresh(tracker)
    return reads.list_states(tracker)


def signature_findings(tracker: str) -> list:
    """Store-wide signature integrity: certify every ticket's recorded signature.

    Reduces each ticket RAW (the public list strips the HMAC hex, so verification
    must read the reducer state directly) and recomputes the HMAC with this
    environment's key. A tampered manifest → MAJOR; a signature made by a DIFFERENT
    environment → MINOR (can't be certified here). ``certified``/``unsigned`` emit
    nothing. When this environment has no key (read-only / foreign clone) the check
    no-ops rather than flagging everything foreign — absence of a key is not an
    integrity failure. Operates on closed tickets too (signatures gate closure),
    which ``normalize_issues`` drops — hence the separate raw pass.
    """
    from rebar import signing
    from rebar._engine_support.validate_checks import Finding
    from rebar.reducer import reduce_ticket

    out: list = []
    try:
        key = signing.signing_key(tracker, create_if_missing=False)
    except Exception:  # noqa: BLE001 — fail-open: no key means nothing to certify, return empty
        return out
    if not key:  # _NO_KEY: nothing to certify against here.
        return out
    try:
        entries = sorted(os.listdir(tracker))
    except OSError:
        return out
    for name in entries:
        if name.startswith("."):
            continue
        tdir = os.path.join(tracker, name)
        if not os.path.isdir(tdir):
            continue
        # Cheap pre-filter: only reduce tickets that actually carry a signature
        # event, so an unsigned store costs nothing here.
        try:
            if not any(f.endswith("-SIGNATURE.json") for f in os.listdir(tdir)):
                continue
        except OSError:
            continue
        try:
            state = reduce_ticket(tdir)
        except Exception:  # noqa: BLE001 — never let one bad ticket fail the scan
            continue
        record = signing.most_recent_attestation(state or {})
        if not record:
            continue
        verdict = signing.verify_record(record, name, key).get("verdict")
        if verdict == "mismatch":
            out.append(
                Finding(
                    "major",
                    f"[SIGNATURE] {name}: signature does not match its verified-steps "
                    f"manifest (tampered or invalid)",
                )
            )
        elif verdict == "foreign_key":
            out.append(
                Finding(
                    "minor",
                    f"[SIGNATURE] {name}: signed by a different environment (cannot certify here)",
                )
            )
    return out


def normalize_issues(tickets: list[dict]) -> list[dict]:
    """Port of the ``get_shared_issues_json`` heredoc: drop error/closed/[LOCK]
    tickets and project each onto the internal schema the checks consume."""
    issues = []
    for t in tickets:
        status = t.get("status", "open")
        if status in ("error", "fsck_needed", "closed"):
            continue
        tid = t.get("ticket_id") or t.get("id", "")
        if not tid:
            continue
        title = t.get("title", "")
        if title.startswith("[LOCK]"):
            continue
        itype = t.get("ticket_type") or t.get("type", "task")
        parent = t.get("parent_id") or t.get("parent") or None
        created = t.get("created_at") or t.get("created") or None

        raw_deps = t.get("deps", t.get("dependencies", []))
        deps = []
        for d in raw_deps:
            dep_id = d.get("target_id") or d.get("depends_on_id", "")
            dep_type = d.get("relation") or d.get("type", "blocks")
            if dep_type == "child_of":
                dep_type = "parent-child"
            if dep_id:
                deps.append({"depends_on_id": dep_id, "type": dep_type})

        description = t.get("description", "")
        if not description:
            body = t.get("body", "") or ""
            description = "yes" if body.strip() else ""
        notes = t.get("notes", "")
        if not notes:
            notes = "yes" if t.get("comments", []) else ""

        issues.append(
            {
                "id": tid,
                "title": title,
                "status": status,
                "type": itype or "task",
                "parent": parent,
                "dependencies": deps,
                "created": created,
                "description": description,
                "notes": notes,
                "tags": t.get("tags", []),
            }
        )
    return issues


# ───────────────────────────── checks + scoring ──────────────────────────────
# Each bash check calls get_shared_issues_json, whose "Fetching..." verbose line
# re-emits per call (its cache lives in a pipe-subshell and never persists). The
# port fetches once, so to keep verbose byte-parity we splice this line in after
# each check's opening "Checking..." verbose finding.
_FETCHING = _checks.Finding("verbose", "Reading local tickets (in-process)...")


def _with_fetch(findings: list[_checks.Finding]) -> list[_checks.Finding]:
    return [findings[0], _FETCHING, *findings[1:]] if findings else findings


def run_checks(issues: list[dict], *, quick: bool, ticket_cmd: str) -> list[_checks.Finding]:
    """Run the checks in the fixed bash order and return the flat finding stream
    (verbose lines included, in emission order)."""
    findings: list[_checks.Finding] = []
    findings += _with_fetch(_checks.check_orphaned_tasks(issues))
    findings += _with_fetch(_checks.check_empty_epics(issues))
    findings += _with_fetch(_checks.check_ticket_count(issues))
    findings += _with_fetch(_checks.check_child_parent_deps(issues))
    findings += _with_fetch(_checks.check_cross_epic_child_deps(issues))
    findings += _with_fetch(_checks.check_duplicate_titles(issues))
    if not quick:
        findings += _with_fetch(_checks.check_missing_descriptions(issues))
        findings += _with_fetch(_checks.check_interface_contracts(issues, ticket_cmd))
        findings += _with_fetch(_checks.check_in_progress_without_notes(issues))
    return findings


def _bucket(findings: list[_checks.Finding]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {
        "critical": [],
        "major": [],
        "minor": [],
        "warning": [],
        "suggestion": [],
    }
    for sev, msg in findings:
        if sev in out:
            out[sev].append(msg)
    return out


def calculate_score(buckets: dict[str, list[str]]) -> int:
    """Port of ``calculate_score``: -2 per critical, -1 per 2 major, -1 per 5 minor,
    -1 per 10 warnings; clamped to [1, 5]."""
    critical = len(buckets["critical"])
    major = len(buckets["major"])
    minor = len(buckets["minor"])
    warnings = len(buckets["warning"])
    score = 5
    if critical > 0:
        score -= critical * 2
    if major > 0:
        score -= (major + 1) // 2
    if minor > 0:
        score -= (minor + 4) // 5
    if warnings > 0:
        score -= (warnings + 9) // 10
    return max(1, min(5, score))


# ───────────────────────────── rendering ─────────────────────────────────────
def to_json_dict(score: int, buckets: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "score": score,
        "critical_issues": buckets["critical"],
        "major_issues": buckets["major"],
        "minor_issues": buckets["minor"],
        "warnings": buckets["warning"],
        "suggestions": buckets["suggestion"],
    }


def _emit_findings(findings: list[_checks.Finding], *, verbose: bool) -> None:
    """Print findings to stderr in emission order, with color (the log_* helpers).
    Verbose/suggestion lines appear only under --verbose."""
    for sev, msg in findings:
        if sev not in _ALWAYS_SHOWN and not verbose:
            continue
        color, prefix = _SEV_RENDER[sev]
        print(f"{color}{prefix}{_NC} {msg}", file=sys.stderr)


def _emit_summary(score: int, buckets: dict[str, list[str]], *, terse: bool) -> None:
    c, ma, mi, w = (
        len(buckets["critical"]),
        len(buckets["major"]),
        len(buckets["minor"]),
        len(buckets["warning"]),
    )
    if terse and score == 5:
        print(
            f"Issues health: {score}/5 ({c} critical, {ma} major, {mi} minor, {w} warnings)",
            file=sys.stderr,
        )
        return
    print("", file=sys.stderr)
    print(f"{_BLUE}=== Summary ==={_NC}", file=sys.stderr)
    print(f"Critical issues: {c}", file=sys.stderr)
    print(f"Major issues: {ma}", file=sys.stderr)
    print(f"Minor issues: {mi}", file=sys.stderr)
    print(f"Warnings: {w}", file=sys.stderr)
    print("", file=sys.stderr)
    if terse:
        _labels = {
            4: f"Health Score: {_GREEN}{score}/5{_NC} - Good (minor issues)",
            3: f"Health Score: {_YELLOW}{score}/5{_NC} - Fair (needs attention)",
            2: f"Health Score: {_YELLOW}{score}/5{_NC} - Poor (significant issues)",
            1: f"Health Score: {_RED}{score}/5{_NC} - Critical (immediate action needed)",
        }
        if score in _labels:
            print(_labels[score], file=sys.stderr)
        print("", file=sys.stderr)
        print("Run with --verbose for more details", file=sys.stderr)
        print("Run with --fix to attempt automatic repairs (interactive)", file=sys.stderr)
        return
    _labels = {
        5: f"Health Score: {_GREEN}{score}/5{_NC} - Excellent",
        4: f"Health Score: {_GREEN}{score}/5{_NC} - Good (minor issues)",
        3: f"Health Score: {_YELLOW}{score}/5{_NC} - Fair (needs attention)",
        2: f"Health Score: {_YELLOW}{score}/5{_NC} - Poor (significant issues)",
        1: f"Health Score: {_RED}{score}/5{_NC} - Critical (immediate action needed)",
    }
    print(_labels[score], file=sys.stderr)
    if score < 5:
        print("", file=sys.stderr)
        print("Run with --verbose for more details", file=sys.stderr)
        print("Run with --fix to attempt automatic repairs (interactive)", file=sys.stderr)


# ───────────────────────────── library entrypoint ────────────────────────────
def validate_state(tracker: str, *, quick: bool = False) -> dict[str, Any]:
    """In-process validate for the library/MCP: returns the JSON report dict
    ({score, critical_issues, major_issues, minor_issues, warnings, suggestions})."""
    issues = normalize_issues(_raw_tickets(tracker))
    findings = run_checks(issues, quick=quick, ticket_cmd=_default_ticket_cmd())
    findings += signature_findings(tracker)
    buckets = _bucket(findings)
    return to_json_dict(calculate_score(buckets), buckets)


# ───────────────────────────── CLI entrypoint ────────────────────────────────
_HELP = """Usage: rebar validate [--quick] [--full] [--fix] [--verbose] [--output json] [--terse]

Options:
  --quick        Run only the fast, high-value checks (~2 seconds)
  --full         Run all checks (default, same as no flag)
  --fix          Attempt to automatically fix issues (interactive)
  --verbose      Show detailed output
  --output json  Emit results as JSON (-o json; default is human text)
  --terse        Single-line output on success; multi-line only when issues exist"""


def run(argv: list[str], tracker: str) -> int:
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    json_output = fmt == "json"
    verbose = False
    quick = False
    terse = False
    for arg in rest:
        if arg in ("--verbose", "-v"):
            verbose = True
        elif arg == "--fix":
            pass  # accepted, no-op (the bash --fix path is unimplemented)
        elif arg == "--quick":
            quick = True
        elif arg == "--full":
            quick = False
        elif arg == "--terse":
            terse = True
        elif arg in ("--help", "-h"):
            print(_HELP)
            return 0
        else:
            print(f"Unknown option: {arg}")
            return 1

    # Header (text, full mode only — not terse, not json).
    if not json_output and not terse:
        print(f"{_BLUE}=== Issue Tracking Health Check ==={_NC}", file=sys.stderr)
        if quick:
            print(f"{_YELLOW}(quick mode — run --full for complete check){_NC}", file=sys.stderr)
        print("", file=sys.stderr)

    ticket_cmd = _default_ticket_cmd()
    issues = normalize_issues(_raw_tickets(tracker))
    findings = run_checks(issues, quick=quick, ticket_cmd=ticket_cmd)
    findings += signature_findings(tracker)
    buckets = _bucket(findings)
    score = calculate_score(buckets)

    if json_output:
        print(json.dumps(to_json_dict(score, buckets), indent=2))
    else:
        _emit_findings(findings, verbose=verbose)
        _emit_summary(score, buckets, terse=terse)

    return 5 - score
