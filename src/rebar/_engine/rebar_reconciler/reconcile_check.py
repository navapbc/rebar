"""Read-only reconciliation check: compare all bound ticket pairs and report
discrepancies WITHOUT making any changes.

This is the self-healing diagnostic tool — invoked as:

    python -m rebar_reconciler --mode reconcile-check

The function :func:`reconcile_check` is pure (no I/O besides the return
value); the CLI wiring in ``__main__.py`` handles snapshot loading and
JSON output.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_sibling(module_name: str, file_name: str) -> ModuleType:
    """Load a sibling module under a stable cache key without PYTHONPATH."""
    sibling_path = Path(__file__).parent / file_name
    cache_key = f"rebar_reconciler_{module_name}"
    if cache_key in sys.modules:
        return sys.modules[cache_key]
    spec = importlib.util.spec_from_file_location(cache_key, sibling_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[cache_key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_config() -> ModuleType:
    return _load_sibling("config", "config.py")


# ---------------------------------------------------------------------------
# Field comparison helpers
# ---------------------------------------------------------------------------

_STATUS_LOCAL_TO_JIRA: dict[str, str] | None = None
_STATUS_JIRA_TO_LOCAL: dict[str, str] | None = None


def _status_maps() -> tuple[dict[str, str], dict[str, str]]:
    """Return (local_to_jira, jira_to_local) status mappings, cached."""
    global _STATUS_LOCAL_TO_JIRA, _STATUS_JIRA_TO_LOCAL  # noqa: PLW0603
    if _STATUS_LOCAL_TO_JIRA is None:
        cfg = _load_config()
        _STATUS_LOCAL_TO_JIRA = dict(getattr(cfg, "local_to_jira_status", {}))
        _STATUS_JIRA_TO_LOCAL = {v: k for k, v in _STATUS_LOCAL_TO_JIRA.items()}
    assert _STATUS_JIRA_TO_LOCAL is not None
    return _STATUS_LOCAL_TO_JIRA, _STATUS_JIRA_TO_LOCAL


# Priority mapping: local integer (0-4) ↔ Jira name
_PRIORITY_LOCAL_TO_JIRA: dict[int, str] = {
    0: "Highest",
    1: "High",
    2: "Medium",
    3: "Low",
    4: "Lowest",
}
_PRIORITY_JIRA_TO_LOCAL: dict[str, int] = {
    v: k for k, v in _PRIORITY_LOCAL_TO_JIRA.items()
}

# Issue type mapping: local type ↔ Jira issuetype
_TYPE_LOCAL_TO_JIRA: dict[str, str] = {
    "epic": "Epic",
    "story": "Story",
    "task": "Task",
    "bug": "Bug",
}
_TYPE_JIRA_TO_LOCAL: dict[str, str] = {v: k for k, v in _TYPE_LOCAL_TO_JIRA.items()}


def _is_rebar_internal_label(label: str) -> bool:
    """Return True for labels that should be excluded from comparison."""
    return label.startswith("rebar-id-") or label.startswith("imported:")


def _compare_labels(
    local_labels: list[str] | None,
    jira_labels: list[str] | None,
) -> list[dict[str, Any]]:
    """Compare labels (excluding rebar-id-* and imported:*), return discrepancies."""
    local_set = {lbl for lbl in (local_labels or []) if not _is_rebar_internal_label(lbl)}
    jira_set = {lbl for lbl in (jira_labels or []) if not _is_rebar_internal_label(lbl)}
    if local_set == jira_set:
        return []
    return [
        {
            "field": "labels",
            "local_value": sorted(local_set),
            "jira_value": sorted(jira_set),
        }
    ]


def _values_match_with_mapping(
    field: str,
    local_val: Any,
    jira_val: Any,
) -> bool:
    """Return True when local and jira values are equivalent under known mappings."""
    if local_val == jira_val:
        return True

    if field == "status":
        l2j, _ = _status_maps()
        return l2j.get(str(local_val)) == jira_val

    if field == "priority":
        try:
            local_int = int(local_val) if local_val is not None else None
        except (TypeError, ValueError):
            local_int = None
        if local_int is not None:
            return _PRIORITY_LOCAL_TO_JIRA.get(local_int) == jira_val
        return False

    if field == "issuetype":
        return _TYPE_LOCAL_TO_JIRA.get(str(local_val)) == jira_val

    return False


# Fields compared on each bound pair.  "title"↔"summary" is handled specially.
_COMPARABLE_FIELDS: tuple[str, ...] = (
    "description",
    "status",
    "priority",
    "issuetype",
    "assignee",
)


def _compare_pair(
    local_id: str,
    jira_key: str,
    local_ticket: dict[str, Any],
    jira_issue: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compare one bound pair and return a list of field discrepancies."""
    discs: list[dict[str, Any]] = []

    # title ↔ summary
    local_title = local_ticket.get("title", local_ticket.get("summary"))
    jira_summary = jira_issue.get("summary", jira_issue.get("title"))
    if (
        local_title != jira_summary
        and local_title is not None
        and jira_summary is not None
    ):
        discs.append(
            {
                "local_id": local_id,
                "jira_key": jira_key,
                "field": "title",
                "local_value": local_title,
                "jira_value": jira_summary,
            }
        )

    for field in _COMPARABLE_FIELDS:
        local_val = local_ticket.get(field)
        jira_val = jira_issue.get(field)
        if local_val is None and jira_val is None:
            continue
        if not _values_match_with_mapping(field, local_val, jira_val):
            discs.append(
                {
                    "local_id": local_id,
                    "jira_key": jira_key,
                    "field": field,
                    "local_value": local_val,
                    "jira_value": jira_val,
                }
            )

    # Labels — local tickets use "tags"; Jira issues use "labels"
    for ld in _compare_labels(
        local_ticket.get("tags"),
        jira_issue.get("labels"),
    ):
        discs.append({"local_id": local_id, "jira_key": jira_key, **ld})

    return discs


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def reconcile_check(
    local_tickets: list[dict[str, Any]],
    jira_snapshot: dict[str, dict[str, Any]],
    binding_store: Any,
) -> dict[str, Any]:
    """Compare all bound pairs and report discrepancies.

    Args:
        local_tickets: List of local ticket dicts (each must have an ``id``
            field used to match binding_store entries).
        jira_snapshot: ``{jira_key: {field: value, ...}}`` — the Jira
            working-set snapshot.
        binding_store: An object with ``.all_bindings() -> dict[str, dict]``
            returning ``{local_id: {"jira_key": ..., ...}}`` entries.

    Returns:
        A report dict with keys: ``total_bindings``, ``checked``,
        ``in_sync``, ``discrepancies``, ``orphaned_bindings``,
        ``orphaned_jira``, ``unbound_local``, ``unbound_jira``.
    """
    # Build lookup maps
    local_by_id: dict[str, dict[str, Any]] = {}
    for ticket in local_tickets:
        tid = ticket.get("ticket_id") or ticket.get("id", "")
        if tid:
            local_by_id[tid] = ticket

    bindings: dict[str, dict] = binding_store.all_bindings()
    bound_local_ids: set[str] = set()
    bound_jira_keys: set[str] = set()
    discrepancies: list[dict[str, Any]] = []
    orphaned_bindings: list[str] = []
    checked = 0

    for local_id, entry in bindings.items():
        jira_key = entry.get("jira_key", "")
        bound_local_ids.add(local_id)
        bound_jira_keys.add(jira_key)

        local_ticket = local_by_id.get(local_id)
        jira_issue = jira_snapshot.get(jira_key)

        if local_ticket is None:
            orphaned_bindings.append(local_id)
            continue

        if jira_issue is None:
            orphaned_bindings.append(local_id)
            continue

        checked += 1
        pair_discs = _compare_pair(local_id, jira_key, local_ticket, jira_issue)
        discrepancies.extend(pair_discs)

    # Orphaned Jira: issues with rebar-id-* labels but no binding
    orphaned_jira: list[str] = []
    for jira_key, jira_issue in jira_snapshot.items():
        if jira_key in bound_jira_keys:
            continue
        labels = jira_issue.get("labels") or []
        has_rebar_id_label = any(
            lbl.startswith("rebar-id-") for lbl in labels if isinstance(lbl, str)
        )
        if has_rebar_id_label:
            orphaned_jira.append(jira_key)

    # Unbound counts
    unbound_local = sum(
        1
        for t in local_tickets
        if (t.get("ticket_id") or t.get("id", "")) not in bound_local_ids
    )
    unbound_jira = sum(
        1
        for jira_key, jira_issue in jira_snapshot.items()
        if jira_key not in bound_jira_keys
        and not any(
            lbl.startswith("rebar-id-")
            for lbl in (jira_issue.get("labels") or [])
            if isinstance(lbl, str)
        )
    )

    in_sync = checked - len({(d["local_id"], d["jira_key"]) for d in discrepancies})

    return {
        "total_bindings": len(bindings),
        "checked": checked,
        "in_sync": in_sync,
        "discrepancies": discrepancies,
        "orphaned_bindings": orphaned_bindings,
        "orphaned_jira": orphaned_jira,
        "unbound_local": unbound_local,
        "unbound_jira": unbound_jira,
    }


def format_report(report: dict[str, Any]) -> str:
    """Format a reconcile_check report as a human-readable string."""
    lines: list[str] = []
    lines.append(
        f"Reconciliation check: {report['total_bindings']} bindings, "
        f"{report['in_sync']} in sync, "
        f"{len(report['discrepancies'])} discrepancies"
    )
    for d in report["discrepancies"]:
        lines.append(
            f"  {d['jira_key']} ({d['local_id']}): {d['field']} mismatch "
            f"— local={d['local_value']!r} jira={d['jira_value']!r}"
        )
    lines.append(f"Orphaned bindings: {len(report['orphaned_bindings'])}")
    for ob in report["orphaned_bindings"]:
        lines.append(f"  {ob} — bound but missing locally or in Jira")
    lines.append(f"Orphaned Jira issues: {len(report['orphaned_jira'])}")
    for oj in report["orphaned_jira"]:
        lines.append(f"  {oj} — has rebar-id-* label but no local binding")
    lines.append(
        f"Unbound: {report['unbound_local']} local tickets, "
        f"{report['unbound_jira']} Jira issues"
    )
    return "\n".join(lines)


def write_report_json(report: dict[str, Any], output_path: Path) -> None:
    """Write the full report as JSON for programmatic consumption."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
