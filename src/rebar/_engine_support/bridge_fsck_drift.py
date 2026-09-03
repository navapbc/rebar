"""Binding-drift audit and report formatting for bridge fsck.

This is the drift/report call-graph seam of the ``rebar._engine_support.bridge_fsck``
support module:
the offline drift audit and the human-readable report formatter share one
cohesive vocabulary and are extracted together to keep the main facade under
the module-size cap.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON file, returning None on any parse or IO error."""
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - helper fail-open: any parse/IO error yields None
        return None


def _load_classify() -> ModuleType:
    """Load the pure reconciler classifier by path."""
    import importlib.util
    import sys

    src = Path(__file__).resolve().parent.parent / "_engine" / "rebar_reconciler" / "classify.py"
    name = "rebar_reconciler_classify_fsck"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, src)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ALERTING_DRIFT_CLASSES = (
    "would_terminal",
    "local_gone",
    "retired_overlap",
    "unbound_jira",
)
"""Offline-decidable drift classes that must alert and drive a non-zero exit."""


def _empty_binding_drift() -> dict[str, list[dict[str, Any]]]:
    return {
        "would_terminal": [],
        "local_gone": [],
        "retired_overlap": [],
        "dangling": [],
        "orphaned_bindings": [],
        "orphaned_jira": [],
        "unbound_local": [],
        "absent_in_window_unprobed": [],
        "unbound_jira": [],
    }


def _append_orphaned_binding(
    drift: dict[str, list[dict[str, Any]]], *, local_id: str, jira_key: str, reason: str
) -> None:
    drift["orphaned_bindings"].append(
        {"local_id": local_id, "jira_key": jira_key, "reason": reason}
    )


def _append_local_gone(
    drift: dict[str, list[dict[str, Any]]],
    *,
    local_id: str,
    jira_key: str,
) -> None:
    drift["local_gone"].append({"local_id": local_id, "jira_key": jira_key})
    _append_orphaned_binding(
        drift,
        local_id=local_id,
        jira_key=jira_key,
        reason="local_gone",
    )


def _audit_binding_without_snapshot(
    drift: dict[str, list[dict[str, Any]]],
    *,
    local_id: str,
    jira_key: str,
    local: dict[str, Any] | None,
    classify_mod: Any,
) -> None:
    lstate = classify_mod.local_state(local)
    if lstate is classify_mod.LocalState.TERMINAL:
        drift["would_terminal"].append({"local_id": local_id, "jira_key": jira_key})
    elif lstate is classify_mod.LocalState.ABSENT:
        _append_local_gone(drift, local_id=local_id, jira_key=jira_key)


def _audit_present_binding(
    drift: dict[str, list[dict[str, Any]]],
    *,
    local_id: str,
    jira_key: str,
    local: dict[str, Any] | None,
    jira_fields: Any,
    entry: dict[str, Any],
    classify_mod: Any,
) -> None:
    if classify_mod.local_state(local) is classify_mod.LocalState.TERMINAL and jira_fields == {}:
        drift.setdefault("indeterminate", []).append(
            {
                "local_id": local_id,
                "jira_key": jira_key,
                "reason": "jira status unavailable in key-set snapshot",
            }
        )
        return
    obs = classify_mod.JiraObservation(
        classify_mod.ObservedJira.PRESENT,
        key=jira_key,
        fields=jira_fields,
    )
    decision = classify_mod.classify(local, obs, entry, entry.get("baseline"))
    if decision.kind is classify_mod.DecisionKind.TERMINAL_TRANSITION:
        drift["would_terminal"].append({"local_id": local_id, "jira_key": jira_key})
    elif decision.kind is classify_mod.DecisionKind.ALERT:
        _append_local_gone(drift, local_id=local_id, jira_key=jira_key)


def _audit_absent_binding(
    drift: dict[str, list[dict[str, Any]]],
    *,
    local_id: str,
    jira_key: str,
    entry: dict[str, Any],
) -> None:
    absent_404 = int(entry.get("absent_404_count", 0) or 0)
    if absent_404 > 0:
        drift["dangling"].append(
            {"local_id": local_id, "jira_key": jira_key, "absent_404_count": absent_404}
        )
        _append_orphaned_binding(
            drift,
            local_id=local_id,
            jira_key=jira_key,
            reason="confirmed_404",
        )
        return
    drift["absent_in_window_unprobed"].append({"local_id": local_id, "jira_key": jira_key})


def _record_unbound_local(
    drift: dict[str, list[dict[str, Any]]],
    *,
    local_by_id: dict[str, dict[str, Any]],
    bindings: dict[str, Any],
) -> None:
    confirmed_local_ids = {
        local_id
        for local_id, entry in bindings.items()
        if isinstance(entry, dict) and entry.get("state") == "confirmed"
    }
    for local_id in sorted(local_by_id):
        if local_id not in confirmed_local_ids:
            drift["unbound_local"].append({"local_id": local_id})


def _record_unbound_jira(
    drift: dict[str, list[dict[str, Any]]],
    *,
    jira_snapshot: dict[str, Any],
    reverse: dict[str, str] | None,
    classify_mod: Any,
    is_retired: Any,
) -> None:
    bound_keys = set(reverse) if isinstance(reverse, dict) else set()
    for key, issue in jira_snapshot.items():
        if key in bound_keys:
            continue
        if not isinstance(issue, dict):
            continue
        labels = issue.get("labels") or []
        if any(isinstance(label, str) and label.startswith("rebar-id-") for label in labels):
            drift["orphaned_jira"].append({"jira_key": key})
            continue
        obs = classify_mod.JiraObservation(
            classify_mod.ObservedJira.PRESENT,
            key=key,
            fields=issue,
            retired=is_retired(key),
        )
        decision = classify_mod.classify(None, obs, None, None)
        if decision.kind is classify_mod.DecisionKind.ADOPT:
            drift["unbound_jira"].append({"jira_key": key})


def _finalize_binding_drift(
    drift: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    drift["orphaned_bindings"].sort(
        key=lambda entry: (
            entry.get("local_id", ""),
            entry.get("jira_key", ""),
            entry.get("reason", ""),
        )
    )
    drift["orphaned_jira"].sort(key=lambda entry: entry.get("jira_key", ""))
    drift["unbound_local"].sort(key=lambda entry: entry.get("local_id", ""))
    drift["unbound_jira"].sort(key=lambda entry: entry.get("jira_key", ""))
    return drift


def _load_local_by_id(
    tickets_tracker: Path, local_states: list[dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    if local_states is None:
        from rebar.reducer import reduce_all_tickets

        local_states = reduce_all_tickets(str(tickets_tracker))
    local_by_id: dict[str, dict[str, Any]] = {}
    for state in local_states:
        tid = state.get("ticket_id") or state.get("id")
        if tid:
            local_by_id[tid] = state
    return local_by_id


def _resolve_jira_snapshot(
    bridge_state: Path,
    jira_snapshot: dict[str, Any] | None,
    use_prev_snapshot: bool,
) -> tuple[dict[str, Any] | None, bool]:
    if jira_snapshot is None and use_prev_snapshot:
        prev = _read_json(bridge_state / "prev_snapshot.json")
        jira_snapshot = prev if isinstance(prev, dict) else None
    return jira_snapshot, isinstance(jira_snapshot, dict)


def audit_binding_drift(
    tickets_tracker: Path,
    local_states: list[dict[str, Any]] | None = None,
    jira_snapshot: dict[str, Any] | None = None,
    use_prev_snapshot: bool = True,
) -> dict[str, Any]:
    """Project the offline binding-store and snapshot state into drift findings."""
    drift: dict[str, Any] = _empty_binding_drift()
    bridge_state = tickets_tracker / ".bridge_state"
    store = _read_json(bridge_state / "bindings.json")
    if not isinstance(store, dict):
        return drift
    bindings = store.get("bindings")
    reverse = store.get("reverse")
    if not isinstance(bindings, dict):
        return drift

    local_by_id = _load_local_by_id(tickets_tracker, local_states)
    jira_snapshot, have_snapshot = _resolve_jira_snapshot(
        bridge_state, jira_snapshot, use_prev_snapshot
    )
    classify_mod = _load_classify()

    def _is_retired(key: str) -> bool:
        retired = _read_json(bridge_state / "bindings-retired.json")
        if not isinstance(retired, dict):
            return False
        retired_map = retired.get("retired")
        retired_keys = set(retired_map) if isinstance(retired_map, (dict, list)) else set()
        return key in retired_keys

    for local_id, entry in bindings.items():
        if not isinstance(entry, dict) or entry.get("state") != "confirmed":
            continue
        jira_key = entry.get("jira_key")
        if not isinstance(jira_key, str) or not jira_key:
            continue
        local = local_by_id.get(local_id)
        if not have_snapshot:
            _audit_binding_without_snapshot(
                drift,
                local_id=local_id,
                jira_key=jira_key,
                local=local,
                classify_mod=classify_mod,
            )
            continue
        assert jira_snapshot is not None
        if jira_key in jira_snapshot:
            _audit_present_binding(
                drift,
                local_id=local_id,
                jira_key=jira_key,
                local=local,
                jira_fields=jira_snapshot[jira_key],
                entry=entry,
                classify_mod=classify_mod,
            )
            continue
        _audit_absent_binding(
            drift,
            local_id=local_id,
            jira_key=jira_key,
            entry=entry,
        )

    _record_unbound_local(drift, local_by_id=local_by_id, bindings=bindings)
    if jira_snapshot is not None:
        _record_unbound_jira(
            drift,
            jira_snapshot=jira_snapshot,
            reverse=reverse if isinstance(reverse, dict) else None,
            classify_mod=classify_mod,
            is_retired=_is_retired,
        )

    retired = _read_json(bridge_state / "bindings-retired.json")
    if isinstance(retired, dict):
        retired_map = retired.get("retired")
        retired_keys = set(retired_map) if isinstance(retired_map, (dict, list)) else set()
        live_keys = set(reverse) if isinstance(reverse, dict) else set()
        for key in sorted(retired_keys & live_keys):
            drift["retired_overlap"].append({"jira_key": key})

    return _finalize_binding_drift(drift)


def _append_store_integrity_lines(lines: list[str], store_integrity: list[dict[str, Any]]) -> None:
    if not store_integrity:
        return
    lines.append("")
    lines.append("--- Binding Store Integrity ---")
    for entry in store_integrity:
        details = " ".join(f"{key}={value}" for key, value in entry.items() if key != "kind")
        lines.append(f"  {entry['kind']}: {details}")


def _append_alerting_drift_lines(lines: list[str], binding_drift: dict[str, Any]) -> None:
    drift_total = sum(len(binding_drift.get(key, [])) for key in _ALERTING_DRIFT_CLASSES)
    if not drift_total:
        return
    lines.append("")
    lines.append("--- Binding-Level Drift ---")
    for entry in binding_drift.get("would_terminal", []):
        lines.append(
            f"  would_terminal: local={entry['local_id']} jira_key={entry['jira_key']}"
            " (local archived/deleted; Jira would be driven to Done)"
        )
    for entry in binding_drift.get("local_gone", []):
        lines.append(
            f"  local_gone: local={entry['local_id']} jira_key={entry['jira_key']}"
            " (bound but local ticket absent from store)"
        )
    for entry in binding_drift.get("retired_overlap", []):
        lines.append(
            f"  retired_overlap: jira_key={entry['jira_key']}"
            " (present in BOTH live and retired stores)"
        )
    for entry in binding_drift.get("unbound_jira", []):
        lines.append(f"  unbound_jira: jira_key={entry.get('jira_key')}")


def _informational_drift_present(binding_drift: dict[str, Any]) -> bool:
    return any(
        binding_drift.get(key, [])
        for key in (
            "dangling",
            "orphaned_bindings",
            "orphaned_jira",
            "unbound_local",
            "absent_in_window_unprobed",
            "indeterminate",
        )
    )


def _append_informational_drift_lines(lines: list[str], binding_drift: dict[str, Any]) -> None:
    if not _informational_drift_present(binding_drift):
        return
    lines.append("")
    lines.append("--- Binding-Level Drift (informational; not alerting) ---")
    for entry in binding_drift.get("dangling", []):
        lines.append(
            f"  dangling: local={entry.get('local_id')} jira_key={entry.get('jira_key')}"
            " (confirmed-404 candidate; healer-tracked, not yet retired)"
        )
    for entry in binding_drift.get("orphaned_bindings", []):
        lines.append(
            f"  orphaned_bindings: local={entry.get('local_id')}"
            f" jira_key={entry.get('jira_key')} reason={entry.get('reason')}"
        )
    for entry in binding_drift.get("orphaned_jira", []):
        lines.append(f"  orphaned_jira: jira_key={entry.get('jira_key')}")
    for entry in binding_drift.get("unbound_local", []):
        lines.append(f"  unbound_local: local={entry.get('local_id')}")
    for entry in binding_drift.get("absent_in_window_unprobed", []):
        lines.append(
            f"  absent_in_window_unprobed: local={entry.get('local_id')}"
            f" jira_key={entry.get('jira_key')}"
            " (absent from windowed snapshot; NOT a deletion signal — ADR 0028 §1)"
        )
    for entry in binding_drift.get("indeterminate", []):
        lines.append(
            f"  indeterminate: local={entry.get('local_id')}"
            f" jira_key={entry.get('jira_key')}"
            f" ({entry.get('reason')})"
        )


def _format_report(findings: dict[str, Any]) -> str:
    """Format the bridge fsck findings as a human-readable report."""
    unknown_types = findings.get("unknown_event_types", [])
    store_integrity = findings.get("store_integrity", [])
    binding_drift = findings.get("binding_drift") or {}
    drift_total = sum(len(binding_drift.get(key, [])) for key in _ALERTING_DRIFT_CLASSES)

    lines: list[str] = ["=== Bridge FSck Report ==="]
    lines.append(
        f"Store integrity: {len(store_integrity)}"
        if store_integrity
        else "Store integrity: none found"
    )
    lines.append(f"Binding drift: {drift_total}" if drift_total else "Binding drift: none found")
    if unknown_types:
        lines.append(
            "WARN: store contains event types newer than this rebar understands: "
            f"{', '.join(unknown_types)} — upgrade rebar. A reconcile host on an old "
            "binary reduces without them and may push stale state to Jira."
        )

    _append_store_integrity_lines(lines, store_integrity)
    _append_alerting_drift_lines(lines, binding_drift)
    _append_informational_drift_lines(lines, binding_drift)
    if not (store_integrity or drift_total):
        lines.append("")
        lines.append("No issues found.")

    return "\n".join(lines)
