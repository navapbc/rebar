"""In-process quality gates: clarity-check / check-ac / quality-check / summary (Tier E E2).

Ports the four per-ticket gate scripts (ticket-clarity-check.sh,
check-acceptance-criteria.sh, issue-quality-check.sh, issue-summary.sh) to
in-process Python, reusing the single reducer via ``reads.show_state`` /
``reads.deps_state``. Each ``_compute`` returns the structured result the gate's
``--output json`` emits (so the library shares it); the ``*_cli`` wrappers add the
text rendering + exit code for byte-parity with the dispatcher.

The scoring/counting heuristics replicate the bash regex/awk/grep-count semantics
exactly (rule order included), since the gates are byte-pinned contracts.
"""

from __future__ import annotations

import json
import os
import re
import sys

from rebar._engine_support.output import OutputFormatError, parse_output
from rebar._engine_support.reads import ReadError, deps_state, show_state


# ── shared text extraction ────────────────────────────────────────────────────
def _ticket_text(state: dict) -> str:
    """title + description + comment bodies, joined by newlines (the gates' input)."""
    parts: list[str] = []
    if state.get("title"):
        parts.append(state["title"])
    if state.get("description"):
        parts.append(state["description"])
    for c in state.get("comments", []) or []:
        body = c.get("body", "")
        if body:
            parts.append(body)
    return "\n".join(parts)


def _grep_c(pattern: re.Pattern, text: str) -> int:
    """Number of LINES matching ``pattern`` (grep -c semantics)."""
    return sum(1 for ln in text.split("\n") if pattern.search(ln))


def _count_ac_reset(text: str) -> int:
    """`- [` items under `## Acceptance Criteria` (check-ac/clarity awk: reset on next ##)."""
    count, found = 0, False
    for ln in text.split("\n"):
        if ln.lower().startswith("## acceptance criteria"):
            found = True
            continue
        if found and ln.startswith("## "):
            found = False
            continue
        if found and ln.startswith("- ["):
            count += 1
    return count


# ── check-ac ──────────────────────────────────────────────────────────────────
def check_ac_compute(ticket_id: str, tracker: str) -> tuple[dict, int]:
    try:
        state = show_state(ticket_id, tracker)
    except ReadError:
        return {"verdict": "fail", "criteria_count": 0, "reason": f"could not load {ticket_id}"}, 1
    if state.get("ticket_type") == "session_log":
        return {"verdict": "pass", "criteria_count": 0, "reason": "session_log is gate-exempt"}, 0
    n = _count_ac_reset(_ticket_text(state))
    if n >= 1:
        return {"verdict": "pass", "criteria_count": n, "reason": f"{n} criteria lines"}, 0
    return {
        "verdict": "fail",
        "criteria_count": 0,
        "reason": f"no ACCEPTANCE CRITERIA section in {ticket_id}",
    }, 1


def check_ac_cli(argv: list[str], tracker: str) -> int:
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if len(rest) < 1:
        sys.stderr.write("Usage: check-acceptance-criteria.sh <id>\n")
        return 1
    result, code = check_ac_compute(rest[0], tracker)
    if fmt == "json":
        sys.stdout.write(json.dumps(result) + "\n")
    elif result["verdict"] == "pass":
        sys.stdout.write(f"AC_CHECK: pass ({result['criteria_count']} criteria lines)\n")
    elif "could not load" in result["reason"]:
        sys.stdout.write(f"AC_CHECK: fail - could not load {rest[0]}\n")
    else:
        sys.stdout.write(
            f"AC_CHECK: fail - no ACCEPTANCE CRITERIA section in {rest[0]} "
            "(use: rebar create with an '## Acceptance Criteria' section with checklist items)\n"
        )
    return code


# ── clarity-check ─────────────────────────────────────────────────────────────
def _clarity_score(description: str, ticket_type: str) -> int:
    score = 0
    if re.search(r"^##\s+\S", description, re.MULTILINE):
        score += 1
    if len(description) >= 200:
        score += 1
    if len(description) >= 500:
        score += 1
    if re.search(r"^- ", description, re.MULTILINE):
        score += 1
    if ticket_type == "task":
        if re.search(r"^##\s+Acceptance Criteria", description, re.MULTILINE | re.IGNORECASE):
            score += 2
        if re.search(r"(?:^|\s)[\w./]+/[\w./]+", description, re.MULTILINE):
            score += 1
    elif ticket_type == "story":
        has_why = bool(re.search(r"^##\s+Why\b", description, re.MULTILINE | re.IGNORECASE))
        has_what = bool(re.search(r"^##\s+What\b", description, re.MULTILINE | re.IGNORECASE))
        if has_why and has_what:
            score += 2
        if re.search(r"^##\s+Scope\b", description, re.MULTILINE | re.IGNORECASE):
            score += 1
    elif ticket_type == "bug":
        if re.search(r"^##\s+Reproduction Steps", description, re.MULTILINE | re.IGNORECASE):
            score += 2
        if re.search(r"expected|actual", description, re.IGNORECASE):
            score += 1
    elif ticket_type == "epic":
        if re.search(r"^##\s+Success Criteria", description, re.MULTILINE | re.IGNORECASE):
            score += 2
        if re.search(r"^##\s+Context\b", description, re.MULTILINE | re.IGNORECASE):
            score += 1
    return score


def _clarity_threshold(repo_root: str | None, config_file: str | None) -> int:
    """Clarity-check pass threshold, resolved through the typed config
    (``ticket_clarity.threshold``: ``[tool.rebar.ticket_clarity]`` / ``rebar.toml`` /
    legacy ``.rebar/config.conf`` + env ``REBAR_TICKET_CLARITY_THRESHOLD``, default 5).
    An explicit ``config_file`` reads that one file; otherwise the layered loader runs.
    A malformed config falls back to the default — clarity is a non-critical gate."""
    from rebar.config import ConfigError, load_config, read_config_file

    try:
        cfg = read_config_file(config_file) if config_file else load_config(root=repo_root)
        return cfg.ticket_clarity.threshold
    except ConfigError:
        return 5


def clarity_check_compute(ticket_type: str, description: str, threshold: int) -> tuple[dict, int]:
    if ticket_type == "session_log":
        # session_log tickets carry verbose free-form logs, not dispatchable work,
        # so the clarity heuristic does not apply — they are gate-exempt (pass).
        return {"score": 0, "verdict": "pass", "threshold": threshold}, 0
    score = _clarity_score(description, ticket_type)
    ac_count = _count_ac_reset(description)
    if score >= threshold and ac_count >= 1:
        return {"score": score, "verdict": "pass", "threshold": threshold}, 0
    return {"score": score, "verdict": "fail", "threshold": threshold}, 1


def clarity_check_cli(argv: list[str], tracker: str, repo_root: str | None) -> int:
    mode = ""
    ticket_id = ""
    config_file = ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--stdin":
            mode = "stdin"
            i += 1
        elif a == "--config":
            if i + 1 >= len(argv):
                sys.stderr.write("ERROR: --config requires a file path argument\n")
                return 2
            config_file = argv[i + 1]
            i += 2
        elif a.startswith("--"):
            sys.stderr.write(f"ERROR: unknown flag: {a}\n")
            return 2
        else:
            if ticket_id:
                sys.stderr.write(f"ERROR: unexpected argument: {a}\n")
                return 2
            ticket_id = a
            mode = "ticket_id"
            i += 1
    if not mode:
        sys.stderr.write("ERROR: must supply a ticket ID or --stdin\n")
        return 2

    if mode == "stdin":
        raw = sys.stdin.read()
        if not raw:
            sys.stderr.write("ERROR: no JSON received on stdin\n")
            return 2
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            sys.stderr.write("ERROR: malformed ticket JSON\n")
            return 2
    else:
        try:
            data = show_state(ticket_id, tracker)
        except ReadError:
            sys.stderr.write(f"ERROR: failed to retrieve ticket {ticket_id}\n")
            return 2

    if config_file and not os.path.isfile(config_file):
        sys.stderr.write(f"ERROR: config file not found: {config_file}\n")
        return 2

    ticket_type = (data.get("ticket_type") or "").strip()
    description = data.get("description") or ""
    threshold = _clarity_threshold(repo_root, config_file or None)
    result, code = clarity_check_compute(ticket_type, description, threshold)
    sys.stdout.write(json.dumps(result) + "\n")
    return code


# ── quality-check ─────────────────────────────────────────────────────────────
_QC_FILE_PAT = re.compile(r"(src/|tests/|app/|\.py|\.ts|\.js|\.html)")
_QC_CRITERIA_PAT = re.compile(
    r"(must|should|given|when|then|acceptance|criteria|expect|verify|ensure)", re.IGNORECASE
)


def _count_ac_exit(text: str) -> int:
    """`- [` items under `## Acceptance Criteria` (quality awk: EXIT on next ##)."""
    count, found = 0, False
    for ln in text.split("\n"):
        if ln.lower().startswith("## acceptance criteria"):
            found = True
            continue
        if found and ln.startswith("## "):
            break
        if found and ln.startswith("- ["):
            count += 1
    return count


def _count_file_impact_section(text: str) -> int:
    count, found = 0, False
    for ln in text.split("\n"):
        low = ln.lower()
        if low.startswith("## file impact") or low.startswith("### files to modify"):
            found = True
            continue
        if not found:
            continue
        if ln.startswith("## "):
            break
        if ln.startswith("### ") and not low.startswith("### files to"):
            break
        if _QC_FILE_PAT.search(ln):
            count += 1
    return count


def quality_check_compute(ticket_id: str, tracker: str) -> tuple[dict, int, str | None]:
    """Returns (result, exit_code, stderr_warning_or_None)."""
    try:
        state = show_state(ticket_id, tracker)
    except ReadError:
        return (
            {
                "verdict": "fail",
                "line_count": 0,
                "keyword_count": 0,
                "ac_items": 0,
                "file_impact": 0,
                "reason": f"could not load issue {ticket_id}",
            },
            1,
            None,
        )
    if state.get("ticket_type") == "session_log":
        return (
            {
                "verdict": "pass",
                "line_count": 0,
                "keyword_count": 0,
                "ac_items": 0,
                "file_impact": 0,
                "reason": "session_log is gate-exempt",
            },
            0,
            None,
        )
    ticket_type = state.get("ticket_type") or "task"
    text = _ticket_text(state)
    line_count = _grep_c(re.compile(r"[^ ]"), text)
    keyword_count = _grep_c(_QC_FILE_PAT, text) + _grep_c(_QC_CRITERIA_PAT, text)
    ac_items = _count_ac_exit(text)
    file_impact = _count_file_impact_section(text)
    if file_impact == 0:
        # Supplement: structured FILE_IMPACT events.
        from rebar._engine_support.field_reads import file_impact as _fi

        fi = _fi(ticket_id, tracker)
        if len(fi) > 0:
            file_impact = len(fi)

    def _r(verdict: str, reason: str) -> dict:
        return {
            "verdict": verdict,
            "line_count": line_count,
            "keyword_count": keyword_count,
            "ac_items": ac_items,
            "file_impact": file_impact,
            "reason": reason,
        }

    if ticket_type == "story":
        if line_count >= 5 and keyword_count >= 1:
            return _r("pass", "story - prose done-definitions"), 0, None
        return _r("fail", f"description too sparse ({line_count} lines)"), 1, None

    if ac_items >= 1:
        return _r("pass", f"{ac_items} AC items, {file_impact} file impact"), 0, None
    if file_impact >= 1:
        return _r("pass", f"{file_impact} file impact"), 0, None
    if line_count >= 5 and keyword_count >= 1:
        warn = (
            "WARNING: Task lacks Acceptance block and File Impact section. "
            "Add via 'rebar comment <id> <note>'."
        )
        return _r("pass", "legacy - no AC/file impact"), 0, warn
    return _r("fail", f"description too sparse ({line_count} lines)"), 1, None


def quality_check_cli(argv: list[str], tracker: str) -> int:
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if len(rest) != 1:
        sys.stderr.write("Usage: issue-quality-check.sh <id>\n")
        return 1
    result, code, warn = quality_check_compute(rest[0], tracker)
    lc, kc, ac, fi = (
        result["line_count"],
        result["keyword_count"],
        result["ac_items"],
        result["file_impact"],
    )
    if fmt == "json":
        sys.stdout.write(json.dumps(result) + "\n")
    else:
        reason = result["reason"]
        if result["verdict"] == "fail":
            if reason.startswith("could not load"):
                sys.stdout.write(f"QUALITY: fail - {reason}\n")
            else:
                sys.stdout.write(
                    f"QUALITY: fail - description too sparse ({lc} lines); "
                    "add detail before dispatch\n"
                )
        elif reason == "story - prose done-definitions":
            sys.stdout.write(
                f"QUALITY: pass (story - prose done-definitions) ({lc} lines, {kc} criteria)\n"
            )
        elif reason == "legacy - no AC/file impact":
            sys.stdout.write(
                f"QUALITY: pass (legacy - no AC/file impact) ({lc} lines, {kc} criteria)\n"
            )
        elif ac >= 1:
            sys.stdout.write(
                f"QUALITY: pass ({lc} lines, {kc} criteria, {ac} AC items, {fi} file impact)\n"
            )
        else:
            sys.stdout.write(f"QUALITY: pass ({lc} lines, {kc} criteria, {fi} file impact)\n")
    if warn:
        sys.stderr.write(warn + "\n")
    return code


# ── summary ───────────────────────────────────────────────────────────────────
def summary_compute(ticket_id: str, tracker: str) -> dict:
    try:
        state = show_state(ticket_id, tracker)
    except ReadError:
        return {
            "ticket_id": ticket_id,
            "status": "unknown",
            "title": None,
            "blocking_summary": None,
        }
    title = state.get("title") or "untitled"
    status = state.get("status") or "unknown"
    blockers, ready = [], True
    try:
        deps = deps_state(ticket_id, tracker)
        blockers = deps.get("blockers") or []
        ready = bool(deps.get("ready_to_work", True))
    except Exception:
        # Safe broad catch (story lean-sloth-ham verified): this feeds only the
        # cosmetic ``blocking_summary`` string below, never a gating/lifecycle
        # decision. A degraded dep computation falls back to the "ready" summary — a
        # display default, not a wrong control-flow answer.
        pass
    suffix = ("blocked by: " + " ".join(blockers)) if (blockers and not ready) else "ready"
    return {"ticket_id": ticket_id, "status": status, "title": title, "blocking_summary": suffix}


def summary_cli(argv: list[str], tracker: str) -> int:
    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if len(rest) == 0:
        sys.stderr.write("Usage: issue-summary.sh <id> [<id> ...]\n")
        return 1
    items = [summary_compute(tid, tracker) for tid in rest]
    if fmt == "json":
        sys.stdout.write(json.dumps(items) + "\n")
    else:
        for it in items:
            if it["status"] == "unknown" and it["title"] is None:
                sys.stdout.write(f"{it['ticket_id']} [unknown]\n")
            else:
                sys.stdout.write(
                    f"{it['ticket_id']} [{it['status']}] {it['title']} ({it['blocking_summary']})\n"
                )
    return 0
