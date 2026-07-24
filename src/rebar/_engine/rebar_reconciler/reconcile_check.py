"""Read-only reconciliation check: compare all bound ticket pairs and report
discrepancies WITHOUT making any changes.

This is the self-healing diagnostic tool — invoked as:

    python -m rebar_reconciler --mode reconcile-check

The function :func:`reconcile_check` is pure (no I/O besides the return
value); the CLI wiring in ``__main__.py`` handles snapshot loading and
JSON output.

Ticket ad44: field comparison is canonicalized the same way 625b canonicalized
the outbound differ. Each bound Jira snapshot entry is mapped to LOCAL shape
via the injected backend's ``InboundMapper`` (``backend.inbound.map_remote_to_local``)
and compared against the local ticket in local vocabulary; the local
description is fit through the injected ``OutboundMapper``
(``backend.outbound.map_fields_to_remote``) before the text compare, mirroring
the canonical outbound differ (``outbound_field_diff.py``). This module
therefore imports NOTHING from ``adapters.jira`` and by-path-loads no
``adapters/jira/*`` sibling — the backend is resolved via the neutral
``select_backend`` registry seam (``None`` default) or injected directly by
the caller for tests.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

# ``lazy_load`` centralizes the by-path sibling-loader idiom (rebar_reconciler/
# _loader.py). Import it normally when package context exists, else bootstrap it
# by file path — this module is itself exec'd standalone via
# spec_from_file_location in tests.
try:
    from rebar_reconciler._loader import lazy_load
except ImportError:  # standalone load without package context
    _loader_key = "rebar_reconciler._loader"
    if _loader_key not in sys.modules:
        _loader_spec = importlib.util.spec_from_file_location(
            _loader_key, Path(__file__).parent / "_loader.py"
        )
        assert _loader_spec is not None and _loader_spec.loader is not None
        _loader_mod = importlib.util.module_from_spec(_loader_spec)
        sys.modules[_loader_key] = _loader_mod
        _loader_spec.loader.exec_module(_loader_mod)  # type: ignore[union-attr]
    lazy_load = sys.modules[_loader_key].lazy_load

# Ticket ad44: the shape-tolerant assignee equality is the canonical-shape
# helper 625b's outbound differ already established — reuse it rather than
# reimplement it. ``outbound_field_diff`` is core (imports nothing from
# ``adapters.jira``), so this is a normal package import, not a by-path load.
from rebar_reconciler.outbound_field_diff import _assignee_matches  # noqa: E402


def _load_sibling(module_name: str, file_name: str) -> ModuleType:
    """Load a sibling module under a stable cache key without PYTHONPATH."""
    return lazy_load(f"rebar_reconciler_{module_name}", file_name)


def _load_classify() -> ModuleType:
    """Load the pure convergence classifier (epic 3006-e198, child 8de5).

    reconcile_check is the ONLINE arm of the ONE classifier: its lifecycle
    findings (orphaned bindings, unbound Jira) delegate to ``classify()`` instead
    of bespoke None-checks, so the report and the live pass share one decision
    surface. Field-level comparison (``_compare_pair``) stays here.
    """
    return _load_sibling("classify", "classify.py")


def _load_inbound_differ() -> ModuleType:
    """Load the inbound differ for its canonical bridge-internal label prefixes."""
    return _load_sibling("inbound_differ", "inbound_differ.py")


# ---------------------------------------------------------------------------
# Field comparison helpers
# ---------------------------------------------------------------------------


def _is_rebar_internal_label(label: str) -> bool:
    """Return True for labels that should be excluded from comparison.

    Bug runny-lens-strafe: this must match the differ's exclusion set exactly —
    ``inbound_differ._EXCLUDED_PREFIXES`` = ``("rebar-id:", "rebar-id-",
    "imported:", "rebar-status:")``. The old two-prefix set omitted the
    canonical colon-form ``rebar-id:`` and the reconciler-managed
    ``rebar-status:`` annotation labels, so those bridge-internal labels (which
    the differ never syncs) were falsely flagged as label discrepancies on
    every bound ticket.
    """
    prefixes = _load_inbound_differ()._EXCLUDED_PREFIXES
    return any(label.startswith(p) for p in prefixes)


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


# Fields compared on each bound pair.  "title"↔"summary" is handled specially.
# Bug runny-lens-strafe: "issuetype" is DELIBERATELY absent — it is a
# sync-excepted field the inbound differ never dispatches (Jira's coarse
# Bug/Story/Task/Epic taxonomy is not a faithful reverse-map for the richer
# local types), so comparing it here only ever produced false discrepancies.
_COMPARABLE_FIELDS: tuple[str, ...] = (
    "description",
    "status",
    "priority",
    "assignee",
)


def _compare_pair(
    local_id: str,
    jira_key: str,
    local_ticket: dict[str, Any],
    jira_issue: dict[str, Any],
    backend: Any,
) -> list[dict[str, Any]]:
    """Compare one bound pair and return a list of field discrepancies.

    Ticket ad44: the Jira snapshot entry is canonicalized ONCE via the injected
    backend's ``InboundMapper`` (the same port 625b's canonical outbound differ
    uses) and every field is then compared in LOCAL vocabulary — no raw Jira
    shape (nested ``{"name": ...}`` objects, ADF description dicts, assignee
    dicts) is ever compared against a local scalar. The reported ``jira_value``
    stays the RAW snapshot value (what is actually stored in Jira); only the
    comparison itself moves to canonical shape.
    """
    discs: list[dict[str, Any]] = []
    canonical = backend.inbound.map_remote_to_local(jira_issue)

    # title ↔ summary
    local_title = local_ticket.get("title", local_ticket.get("summary"))
    jira_summary = jira_issue.get("summary", jira_issue.get("title"))
    if local_title != jira_summary and local_title is not None and jira_summary is not None:
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

        if field == "assignee":
            # Neutral membership match: local matches when it equals ANY
            # non-None canonical identity form ({display, email, account_id})
            # or the canonical scalar assignee; both-empty also matches.
            matches = _assignee_matches(
                str(local_val or ""),
                canonical.get("assignee"),
                canonical.get("assignee_identity"),
            )
        elif field == "description":
            # Fit the local text through the OUTBOUND port exactly as the
            # canonical differ does (outbound_field_diff.py) — NOT
            # FieldSanitizer.sanitize_description, which is send-side and
            # emits a truncation WARNING (wrong for a read-only diagnostic).
            fitted = backend.outbound.map_fields_to_remote(
                {"description": local_ticket.get("description") or ""},
                ticket=local_ticket,
            ).get("description", local_val)
            matches = str(fitted or "").rstrip() == str(canonical.get("description") or "").rstrip()
        else:
            # status / priority: the InboundMapper already applied the
            # local<->Jira value mapping (and, for status, the
            # rebar-status:blocked/cancelled annotation-label precedence — see
            # inbound_fields._map_jira_to_local_fields). A field the mapper
            # does not emit (e.g. an unmapped remote status) is treated as
            # divergent, preserving today's behavior.
            matches = field in canonical and local_val == canonical[field]

        if not matches:
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
# Local-ticket loading
# ---------------------------------------------------------------------------


def load_local_tickets(tracker_dir: Path) -> list[dict[str, Any]]:
    """Load compiled local tickets from a ``.tickets-tracker`` directory.

    The event-sourced store writes NO per-ticket ``ticket.json`` snapshot — a
    ticket directory holds its event log (``*-CREATE.json`` etc.) plus a
    compiled ``.cache.json`` whose ``state`` key is the reduced ticket (fields
    keyed ``ticket_id``/``status``/…). Bug ad39: the previous reconcile-check
    loader read ``<id>/ticket.json``, which never exists, so it loaded zero
    local tickets and reported EVERY binding as orphaned. Read ``.cache.json``
    ``state`` instead; a directory with no readable compiled state is skipped
    (it contributes no local ticket, exactly as before).
    """
    tickets: list[dict[str, Any]] = []
    if not tracker_dir.is_dir():
        return tickets
    for entry in sorted(tracker_dir.iterdir()):
        if not entry.is_dir() or ".scratch" in entry.parts:
            continue
        cache_path = entry / ".cache.json"
        if not cache_path.exists():
            continue
        try:
            state = json.loads(cache_path.read_text()).get("state")
        except (ValueError, OSError):
            continue  # unreadable/corrupt cache → skip (no local ticket)
        if not isinstance(state, dict):
            continue
        ticket = dict(state)
        # The compiled state carries ``ticket_id``; keep ``id`` too so both the
        # reconcile_check matcher (ticket_id-or-id) and legacy callers resolve.
        ticket.setdefault("ticket_id", entry.name)
        ticket.setdefault("id", ticket["ticket_id"])
        tickets.append(ticket)
    return tickets


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def reconcile_check(
    local_tickets: list[dict[str, Any]],
    jira_snapshot: dict[str, dict[str, Any]],
    binding_store: Any,
    backend: Any = None,
) -> dict[str, Any]:
    """Compare all bound pairs and report discrepancies.

    Args:
        local_tickets: List of local ticket dicts (each must have an ``id``
            field used to match binding_store entries).
        jira_snapshot: ``{jira_key: {field: value, ...}}`` — the Jira
            working-set snapshot.
        binding_store: An object with ``.all_bindings() -> dict[str, dict]``
            returning ``{local_id: {"jira_key": ..., ...}}`` entries.
        backend: The injected Backend-port object (ticket ad44) whose
            ``inbound``/``outbound`` mappers canonicalize the field compare.
            ``None`` (the default) resolves the configured backend lazily via
            ``select_backend(load_config())`` — mirrors the outbound differ's
            fallback. Tests inject a pure ``JiraBackend(transport=object())``.

    Returns:
        A report dict with keys: ``total_bindings``, ``checked``,
        ``in_sync``, ``discrepancies``, ``orphaned_bindings``,
        ``orphaned_jira``, ``unbound_local``, ``unbound_jira``.
    """
    if backend is None:
        from rebar.config import load_config
        from rebar_reconciler._backend_registry import select_backend

        backend = select_backend(load_config())

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

    # The ONE classifier drives the lifecycle routing (child 8de5). A bound pair
    # whose classifier Decision is ALERT (local gone) or PROBE_GET (Jira gone /
    # absent from the snapshot) is an orphaned binding; a present-present pair
    # (SYNC_FIELDS / TERMINAL_TRANSITION / NOOP) proceeds to the field compare.
    _c = _load_classify()
    _ORPHAN_KINDS = {_c.DecisionKind.ALERT, _c.DecisionKind.PROBE_GET}

    for local_id, entry in bindings.items():
        jira_key = entry.get("jira_key", "")
        bound_local_ids.add(local_id)
        bound_jira_keys.add(jira_key)

        local_ticket = local_by_id.get(local_id)
        jira_issue = jira_snapshot.get(jira_key)

        if jira_issue is not None:
            obs = _c.JiraObservation(_c.ObservedJira.PRESENT, key=jira_key, fields=jira_issue)
        else:
            obs = _c.JiraObservation(_c.ObservedJira.ABSENT_IN_WINDOW, key=jira_key)
        decision = _c.classify(local_ticket, obs, entry, entry.get("baseline"))
        if decision.kind in _ORPHAN_KINDS:
            orphaned_bindings.append(local_id)
            continue

        # Present-present pair — run the field-level comparison.
        checked += 1
        pair_discs = _compare_pair(
            local_id, jira_key, local_ticket or {}, jira_issue or {}, backend
        )
        discrepancies.extend(pair_discs)

    # Orphaned Jira: issues with rebar-id-* labels but no binding (an L10 anomaly —
    # a labeled issue whose binding record was lost; distinct from the classifier's
    # ADOPT cell for label-less native issues, counted as unbound_jira below).
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
        1 for t in local_tickets if (t.get("ticket_id") or t.get("id", "")) not in bound_local_ids
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
        f"Unbound: {report['unbound_local']} local tickets, {report['unbound_jira']} Jira issues"
    )
    return "\n".join(lines)


def write_report_json(report: dict[str, Any], output_path: Path) -> None:
    """Write the full report as JSON for programmatic consumption."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
