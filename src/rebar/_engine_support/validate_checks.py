"""The repo-wide health checks for ``validate`` (Tier C, ``REBAR_COMPUTE``).

Faithful port of the nine ``check_*`` shell functions + their ``python3`` heredocs
in ``validate-issues.sh``. Each check is a pure function over the normalized issue
list (see :func:`rebar._engine_support.validate.normalize_issues`) returning an
ordered list of ``Finding(severity, message)`` in the EXACT emission order the bash
heredoc/wrapper printed them — the orchestrator concatenates the checks in a fixed
order and buckets the flat stream stably, reproducing the bash severity arrays
byte-for-byte (the §5 ordering-determinism requirement). Split out of ``validate``
on the obvious seam: checks take data and yield findings; the orchestrator
scores/renders.

Severity is one of: ``critical``, ``major``, ``minor``, ``warning``, ``suggestion``.
``verbose`` findings mirror the bash ``log_verbose`` debug lines — shown only in
``--verbose`` text mode and never counted toward the score; they are embedded here
at the exact position bash emitted them so the verbose stream is byte-faithful too.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import NamedTuple


class Finding(NamedTuple):
    severity: str  # critical | major | minor | warning | suggestion | verbose
    message: str


def check_orphaned_tasks(issues: list[dict]) -> list[Finding]:
    """Open non-epic/non-bug issues with no parent epic → WARNING each; dense
    creation-hour clusters (≥3) → MAJOR."""
    out: list[Finding] = [Finding("verbose", "Checking for orphaned tasks (no parent epic)...")]
    orphans = []
    for issue in issues:
        itype = issue.get("type", issue.get("issue_type", "task"))
        if itype in ("epic", "bug"):
            continue
        if issue.get("status", "open") == "closed":
            continue
        parent = issue.get("parent", issue.get("parent_id", None))
        deps = issue.get("dependencies", issue.get("deps", []))
        is_child = bool(parent) or any(
            dep.get("dependency_type") == "parent-child" or dep.get("type") == "parent-child"
            for dep in deps
        )
        if not is_child:
            tags = issue.get("tags", [])
            if "orphan:deferred_review" in tags and "origin:arbiter" in tags:
                continue
            orphans.append(issue)

    clusters: dict[str, list[dict]] = defaultdict(list)
    for o in orphans:
        created = o.get("created_at", o.get("created", ""))
        try:
            if isinstance(created, int):
                ts = str(datetime.fromtimestamp(created))[:19]
            else:
                ts = created[:19].replace("T", " ")
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            clusters[dt.strftime("%Y-%m-%d %H:00")].append(o)
        except (ValueError, IndexError, TypeError, OSError, OverflowError):
            pass

    for o in orphans:
        iid = o.get("id", "?")
        itype = o.get("type", o.get("issue_type", "task"))
        title = o.get("title", o.get("name", "?"))
        out.append(Finding("warning", f"Orphaned {itype} (no epic parent): {iid} - {title}"))

    for hour_key, group in sorted(clusters.items()):
        if len(group) >= 3:
            ids = ", ".join(o.get("id", "?").split("-")[-1] for o in group[:5])
            suffix = f" + {len(group) - 5} more" if len(group) > 5 else ""
            out.append(Finding("major", f"{len(group)} orphaned tasks created around {hour_key} ({ids}{suffix}) — likely need an epic"))

    if not orphans:
        out.append(Finding("verbose", "No orphaned tasks found — all open tasks belong to an epic"))
    return out


def check_empty_epics(issues: list[dict]) -> list[Finding]:
    """Open epics with no children → verbose-only (does NOT affect the score, matching
    bash, where ``check_empty_epics`` calls only ``log_verbose``)."""
    out: list[Finding] = [Finding("verbose", "Checking for epics with 0 children...")]
    epic_ids = set()
    for issue in issues:
        if issue.get("type", issue.get("issue_type", "task")) == "epic" and issue.get("status", "open") != "closed":
            iid = issue.get("id", "")
            if iid:
                epic_ids.add(iid)
    epics_with_children = set()
    for issue in issues:
        parent = issue.get("parent", issue.get("parent_id", None))
        if parent and parent in epic_ids:
            epics_with_children.add(parent)
        for dep in issue.get("dependencies", issue.get("deps", [])):
            if dep.get("type") == "parent-child":
                dep_id = dep.get("depends_on_id", dep.get("id", ""))
                if dep_id in epic_ids:
                    epics_with_children.add(dep_id)
    empty = 0
    for issue in issues:
        if issue.get("type", issue.get("issue_type", "task")) == "epic" and issue.get("status", "open") != "closed":
            iid = issue.get("id", "")
            title = issue.get("title", issue.get("name", "?"))
            if iid and iid not in epics_with_children:
                out.append(Finding("verbose", f"Epic with 0 children: {iid} - {title} (decompose into child tickets when ready)"))
                empty += 1
    if empty == 0:
        out.append(Finding("verbose", "All open epics have children"))
    else:
        out.append(Finding("verbose", f"{empty} epic(s) with 0 children (normal for backlog items)"))
    return out


def check_ticket_count(issues: list[dict]) -> list[Finding]:
    """Total unarchived ticket count: ≥600 → MAJOR, ≥300 → WARNING, else verbose."""
    out: list[Finding] = [Finding("verbose", "Checking total ticket count...")]
    total = len(issues)
    if total >= 600:
        out.append(Finding("major", f"Total ticket count is {total} (≥600) — consider archiving closed tickets to keep the tracker manageable"))
    elif total >= 300:
        out.append(Finding("warning", f"Total ticket count is {total} (≥300) — consider archiving older closed tickets"))
    else:
        out.append(Finding("verbose", f"Total ticket count: {total} (within healthy range)"))
    return out


def _parent_of(issue: dict) -> str | None:
    parent = issue.get("parent", issue.get("parent_id", None))
    if parent:
        return parent
    for dep in issue.get("dependencies", issue.get("deps", [])):
        if dep.get("type") == "parent-child":
            return dep.get("depends_on_id", dep.get("id", None))
    return None


def check_child_parent_deps(issues: list[dict]) -> list[Finding]:
    """A child depending on its own parent (anti-pattern) → CRITICAL each."""
    out: list[Finding] = [Finding("verbose", "Checking for child->parent dependencies...")]
    errors = 0
    for issue in issues:
        iid = issue.get("id", "?")
        deps = issue.get("dependencies", issue.get("deps", []))
        parent_id = _parent_of(issue)
        if parent_id:
            for dep in deps:
                dep_type = dep.get("type", "")
                dep_id = dep.get("depends_on_id", dep.get("id", ""))
                if dep_type != "parent-child" and dep_id == parent_id:
                    title = issue.get("title", issue.get("name", "unknown"))
                    out.append(Finding("critical", f"Child->parent dependency: {iid} depends on its parent {parent_id} - {title}"))
                    errors += 1
    if errors == 0:
        out.append(Finding("verbose", "No child->parent dependency violations found"))
    return out


def check_cross_epic_child_deps(issues: list[dict]) -> list[Finding]:
    """A child depending on a child of a DIFFERENT epic → CRITICAL each."""
    out: list[Finding] = [Finding("verbose", "Checking for cross-epic child dependencies...")]
    parent_map: dict[str, str] = {}
    for issue in issues:
        iid = issue.get("id")
        parent = issue.get("parent", issue.get("parent_id", None))
        if parent:
            parent_map[iid] = parent
        else:
            for dep in issue.get("dependencies", issue.get("deps", [])):
                if dep.get("type") == "parent-child":
                    parent_map[iid] = dep.get("depends_on_id", dep.get("id", ""))
                    break
    errors = 0
    for issue in issues:
        iid = issue.get("id")
        my_parent = parent_map.get(iid)
        if not my_parent:
            continue
        for dep in issue.get("dependencies", issue.get("deps", [])):
            if dep.get("type") == "parent-child":
                continue
            dep_id = dep.get("depends_on_id", dep.get("id", ""))
            dep_parent = parent_map.get(dep_id)
            if dep_parent and dep_parent != my_parent:
                title = issue.get("title", issue.get("name", "unknown"))
                out.append(Finding("critical", f"Cross-epic child dependency: {iid} (child of {my_parent}) depends on {dep_id} (child of {dep_parent}). Use epic-level dependency instead - {title}"))
                errors += 1
    if errors == 0:
        out.append(Finding("verbose", "No cross-epic child dependency violations found"))
    return out


def check_duplicate_titles(issues: list[dict]) -> list[Finding]:
    """Titles shared by ≥2 open tickets → MINOR each (sorted, like ``sort|uniq -d``)."""
    out: list[Finding] = [Finding("verbose", "Checking for duplicate task titles...")]
    counts: dict[str, int] = defaultdict(int)
    for t in (i.get("title", "") for i in issues):
        counts[t] += 1
    dups = sorted(t for t, n in counts.items() if n >= 2 and t)
    for t in dups:
        out.append(Finding("minor", f"Duplicate task title: {t}"))
    if not dups:
        out.append(Finding("verbose", "No duplicate titles found"))
    return out


def check_missing_descriptions(issues: list[dict]) -> list[Finding]:
    """Among the first 20 open tasks, those with no description → WARNING each."""
    out: list[Finding] = [Finding("verbose", "Checking for tasks without descriptions...")]
    checked = 0
    for issue in issues:
        if issue.get("type", issue.get("issue_type", "task")) != "task":
            continue
        if issue.get("status", "open") == "closed":
            continue
        desc = issue.get("description", issue.get("body", "") or "")
        iid = issue.get("id", "?")
        title = issue.get("title", issue.get("name", "?"))
        if not desc or not desc.strip():
            out.append(Finding("warning", f"Task missing description: {iid} - {title}"))
        checked += 1
        if checked >= 20:
            break
    return out


_ALWAYS_MATCH = re.compile(r"\binterface\b|\babstract\b|\bbase class\b|\bABC\b", re.IGNORECASE)
_CONTRACT_MATCH = re.compile(r"\bcontract\b", re.IGNORECASE)
_PROTOCOL_MATCH = re.compile(r"\bprotocol\b", re.IGNORECASE)
_FALSE_POSITIVE_TITLES = re.compile(
    r"\bwiremock\b|contract\s+test|test\s+contract|http\s+contract|model\s+context\s+protocol|mcp\s*\(",
    re.IGNORECASE,
)


def _is_interface_contract_title(title: str) -> bool:
    if _FALSE_POSITIVE_TITLES.search(title):
        return False
    if _ALWAYS_MATCH.search(title):
        return True
    return bool(_CONTRACT_MATCH.search(title) or _PROTOCOL_MATCH.search(title))


def check_interface_contracts(issues: list[dict], ticket_cmd: str) -> list[Finding]:
    """Interface/contract tickets lacking a file path or method reference → WARNING
    + a SUGGESTION (the suggestion embeds ``ticket_cmd`` exactly like bash)."""
    out: list[Finding] = [Finding("verbose", "Checking interface contract tasks for documentation...")]
    for issue in issues:
        if issue.get("status", "open") == "closed":
            continue
        title = issue.get("title", issue.get("name", ""))
        if not _is_interface_contract_title(title):
            continue
        iid = issue.get("id", "?")
        desc = issue.get("description", issue.get("body", "") or "")
        notes = issue.get("notes", "") or ""
        combined = desc + notes
        has_file_path = bool(re.search(r"src/|\.py|\.sh|\.md|docs/contracts|skills/|file path", combined, re.IGNORECASE))
        has_methods = bool(re.search(r"method|function|@abstractmethod", combined, re.IGNORECASE))
        if not has_file_path and not has_methods:
            out.append(Finding("warning", f"Interface task may need documentation: {iid} - {title}"))
            out.append(Finding("suggestion", f"Add notes with: {ticket_cmd} comment {iid} 'Interface in src/.../base.py. Key methods: ...'"))
    return out


def check_in_progress_without_notes(issues: list[dict]) -> list[Finding]:
    """in_progress tickets with no progress notes → WARNING each."""
    out: list[Finding] = [Finding("verbose", "Checking for in-progress tasks without progress notes...")]
    for issue in issues:
        if issue.get("status", "open") != "in_progress":
            continue
        iid = issue.get("id", "?")
        title = issue.get("title", issue.get("name", "?"))
        if not (issue.get("notes", "") or "").strip():
            out.append(Finding("warning", f"In-progress task without notes: {iid} - {title}"))
    return out
