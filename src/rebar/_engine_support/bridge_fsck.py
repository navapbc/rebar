"""Bridge-specific fsck audit (in-process; canonical home).

Audits bridge state without a Jira client. Unknown event types come from the
committed tickets ref without a checkout; binding integrity and drift inspect
the materialized tracker state:
  - Unknown top-level event types on the committed tickets ref
  - Forward/reverse binding-index integrity
  - Existing offline binding-drift classifications

Reached in-process via ``rebar.bridge_fsck()`` and the ``rebar bridge fsck`` CLI
arm; the compatibility ``bridge-fsck`` alias uses the same route. ``main()``
renders the byte-pinned CLI output (text / --output json).

Module interface:
    audit_bridge_mappings(tickets_tracker: Path) -> dict
        Returns ``unknown_event_types``, ``binding_drift``, and
        ``store_integrity``.

Exit codes:
    0 — no issues found
    1 — one or more issues found
    2 — operational scan failure
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from rebar._errors import RebarError
from rebar._mcp_errors import js_safe_dumps
from rebar._store.gitutil import run_git

# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    """Read a JSON file, returning None on any parse or IO error."""
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 — helper fail-open: any parse/IO error yields None (documented contract)
        return None


def _load_classify() -> ModuleType:
    """Load the pure reconciler classifier (leaf, stdlib-only) by path.

    bridge fsck is the SECOND consumer of the one classifier (epic 3006-e198,
    child 8de5): the live pass ACTS on Decisions, this offline audit REPORTS
    them — healing the report-only/healing fork. classify.py lives under the
    hyphen-free reconciler package, so it is loaded via spec_from_file_location
    (the established pattern for reaching reconciler leaves from _engine_support).
    """
    import importlib.util
    import sys

    src = Path(__file__).resolve().parent.parent / "_engine" / "rebar_reconciler" / "classify.py"
    name = "rebar_reconciler_classify_fsck"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, src)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: dataclass annotation resolution (Py 3.14) looks the
    # module up in sys.modules while processing @dataclass at import time.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_TICKETS_REF = "refs/heads/tickets"
_GIT_TIMEOUT_SECONDS = 120
_EVENT_TYPE_GREP_PATTERN = r'"event_type"[[:space:]]*:[[:space:]]*"[^"]+"'
_EVENT_TYPE_MATCH = re.compile(r'^"event_type"\s*:\s*"([^"]+)"$')
_GREP_RECORD = re.compile(
    rf"^{re.escape(_TICKETS_REF)}:(?P<path>[^:]+):(?P<line>\d+):(?P<match>.+)$"
)


def _scan_error(detail: str) -> RebarError:
    message = f"bridge fsck unknown-event scan failed: {detail}"
    return RebarError(message, returncode=2, stderr=message)


def _integrity_error(detail: str) -> RebarError:
    message = f"bridge fsck store-integrity scan failed: {detail}"
    return RebarError(message, returncode=2, stderr=message)


def _read_integrity_store(tickets_tracker: Path) -> dict | None:
    path = tickets_tracker / ".bridge_state" / "bindings.json"
    if not path.exists():
        return None
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise _integrity_error(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(store, dict):
        raise _integrity_error(f"{path.name} is not a top-level JSON object")
    return store


def _finding_id(value: object) -> str:
    """Render a corrupt index value without violating the public finding schema."""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _run_scan_git(
    tickets_tracker: Path,
    *args: str,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    """Run bounded Git for the unknown-event scan and normalize launch failures."""
    try:
        return run_git(  # raw-git-ok: bounded read-only grep/show of the committed tickets ref
            tickets_tracker,
            *args,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise _scan_error(f"{operation} timed out after {_GIT_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise _scan_error(f"git is unavailable during {operation}: {exc}") from exc


def _unknown_event_types_from_ref(tickets_tracker: Path) -> list[str]:
    """Find unknown top-level event types on the committed tickets ref.

    ``git grep`` cheaply locates candidate blobs without a tickets checkout. A
    second ``git show`` pass validates each residual candidate as top-level JSON,
    preventing comment or nested compiled-state text from becoming a finding.
    """
    from rebar.reducer._version import _NON_REPLAY_KNOWN_TYPES, KNOWN_EVENT_TYPES

    known_types = KNOWN_EVENT_TYPES | _NON_REPLAY_KNOWN_TYPES
    grep = _run_scan_git(
        tickets_tracker,
        "grep",
        "-n",
        "-o",
        "-E",
        _EVENT_TYPE_GREP_PATTERN,
        _TICKETS_REF,
        "--",
        "*/*.json",
        operation="git grep",
    )
    if grep.returncode == 1:
        return []
    if grep.returncode != 0:
        diagnostic = (grep.stderr or grep.stdout or "no diagnostic").strip()
        raise _scan_error(f"git grep {_TICKETS_REF} exited {grep.returncode}: {diagnostic}")

    candidate_paths: set[str] = set()
    for raw_line in grep.stdout.splitlines():
        record = _GREP_RECORD.fullmatch(raw_line)
        if record is None:
            raise _scan_error(f"malformed git grep output: {raw_line!r}")
        match = _EVENT_TYPE_MATCH.fullmatch(record.group("match"))
        if match is None:
            raise _scan_error(f"malformed event-type match: {record.group('match')!r}")
        if match.group(1) not in known_types:
            candidate_paths.add(record.group("path"))

    unknown: set[str] = set()
    for path in sorted(candidate_paths):
        shown = _run_scan_git(
            tickets_tracker,
            "show",
            f"{_TICKETS_REF}:{path}",
            operation=f"git show for candidate {path!r}",
        )
        if shown.returncode != 0:
            diagnostic = (shown.stderr or shown.stdout or "no diagnostic").strip()
            raise _scan_error(
                f"cannot read candidate {path!r} (exit {shown.returncode}): {diagnostic}"
            )
        try:
            event = json.loads(shown.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise _scan_error(f"cannot parse candidate {path!r} as JSON: {exc}") from exc
        if not isinstance(event, dict):
            raise _scan_error(f"candidate {path!r} is not a top-level JSON object")
        event_type = event.get("event_type")
        if isinstance(event_type, str) and event_type and event_type not in known_types:
            unknown.add(event_type)
    return sorted(unknown)


def audit_store_integrity(tickets_tracker: Path) -> list[dict]:
    """Validate the bidirectional indexes in ``bindings.json`` read-only."""
    store = _read_integrity_store(tickets_tracker)
    if store is None:
        return []
    missing = object()
    bindings = store.get("bindings", missing)
    reverse = store.get("reverse", missing)
    if bindings is missing:
        bindings = {}
    elif not isinstance(bindings, dict):
        raise _integrity_error("bindings.json field 'bindings' is not an object")
    if reverse is missing:
        reverse = {}
    elif not isinstance(reverse, dict):
        raise _integrity_error("bindings.json field 'reverse' is not an object")

    findings: list[dict] = []
    for local_id in sorted(bindings):
        entry = bindings[local_id]
        if not isinstance(entry, dict) or entry.get("state") != "confirmed":
            continue
        jira_key = entry.get("jira_key")
        if not isinstance(jira_key, str) or not jira_key.strip():
            findings.append({"kind": "forward_missing_jira_key", "local_id": local_id})
            continue
        actual_local_id = reverse.get(jira_key, missing)
        if actual_local_id is missing:
            findings.append(
                {
                    "kind": "forward_missing_reverse",
                    "local_id": local_id,
                    "jira_key": jira_key,
                }
            )
        elif actual_local_id != local_id:
            findings.append(
                {
                    "kind": "forward_reverse_mismatch",
                    "local_id": local_id,
                    "jira_key": jira_key,
                    "actual_local_id": _finding_id(actual_local_id),
                }
            )

    for jira_key in sorted(reverse):
        raw_local_id = reverse[jira_key]
        local_id = _finding_id(raw_local_id)
        entry = bindings.get(raw_local_id, missing) if isinstance(raw_local_id, str) else missing
        if entry is missing or not isinstance(entry, dict):
            findings.append(
                {
                    "kind": "reverse_missing_forward",
                    "local_id": local_id,
                    "jira_key": jira_key,
                }
            )
            continue
        if entry.get("state") != "confirmed":
            findings.append(
                {
                    "kind": "reverse_nonconfirmed_forward",
                    "local_id": local_id,
                    "jira_key": jira_key,
                }
            )
            continue
        forward_jira_key = entry.get("jira_key")
        if forward_jira_key != jira_key:
            findings.append(
                {
                    "kind": "reverse_jira_key_mismatch",
                    "local_id": local_id,
                    "jira_key": jira_key,
                    "forward_jira_key": (
                        forward_jira_key
                        if forward_jira_key is None or isinstance(forward_jira_key, str)
                        else _finding_id(forward_jira_key)
                    ),
                }
            )
    return findings


def audit_bridge_mappings(
    tickets_tracker: Path,
) -> dict:
    """Run the committed-event, binding-drift, and index-integrity audits.

    Args:
        tickets_tracker: Path to the .tickets-tracker directory.
    Returns:
        The three-field bridge fsck result.
    """
    # Binding-level drift (child 8de5): the offline arm of the ONE classifier.
    # Best-effort — a failure here must never break the integrity checks.
    try:
        binding_drift = audit_binding_drift(tickets_tracker)
    except Exception:  # noqa: BLE001 — binding-drift arm is additive; degrade to empty on any error
        binding_drift = _empty_binding_drift()

    return {
        "unknown_event_types": _unknown_event_types_from_ref(tickets_tracker),
        "binding_drift": binding_drift,
        "store_integrity": audit_store_integrity(tickets_tracker),
    }


# Offline-decidable drift classes that MUST alert (drive the canary ticket + a
# non-zero exit). ``dangling`` is deliberately NOT here: it is a deletion
# CANDIDATE the live pass is already tracking/healing (ADR 0028 §2), and
# ``absent_in_window_unprobed`` is pure information — neither is an unhealed
# anomaly, so neither may alert (bug f436). See ``audit_binding_drift``.
_ALERTING_DRIFT_CLASSES = (
    "would_terminal",
    "local_gone",
    "retired_overlap",
    "unbound_jira",
)


def _empty_binding_drift() -> dict:
    return {
        "would_terminal": [],
        "local_gone": [],
        "retired_overlap": [],
        "dangling": [],
        "orphaned_bindings": [],
        "orphaned_jira": [],
        "unbound_local": [],
        # ADR 0028 §1: snapshot-window absence is NOT a deletion signal. The
        # offline audit never probes, so un-probed window-absent bindings land
        # here — informational only, NEVER alerting (see _ALERTING_DRIFT_CLASSES).
        "absent_in_window_unprobed": [],
        "unbound_jira": [],
    }


def _append_orphaned_binding(
    drift: dict[str, Any], *, local_id: str, jira_key: str, reason: str
) -> None:
    drift["orphaned_bindings"].append(
        {"local_id": local_id, "jira_key": jira_key, "reason": reason}
    )


def _append_local_gone(
    drift: dict[str, Any],
    *,
    local_id: str,
    jira_key: str,
    absent_404: int = 0,
) -> None:
    drift["local_gone"].append({"local_id": local_id, "jira_key": jira_key})
    if absent_404 > 0:
        drift["dangling"].append(
            {"local_id": local_id, "jira_key": jira_key, "absent_404_count": absent_404}
        )
    _append_orphaned_binding(
        drift,
        local_id=local_id,
        jira_key=jira_key,
        reason="confirmed_404" if absent_404 > 0 else "local_gone",
    )


def _audit_binding_without_snapshot(
    drift: dict[str, Any],
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
    drift: dict[str, Any],
    *,
    local_id: str,
    jira_key: str,
    local: dict[str, Any] | None,
    jira_fields: dict[str, Any],
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
    drift: dict[str, Any],
    *,
    local_id: str,
    jira_key: str,
    local: dict[str, Any] | None,
    entry: dict[str, Any],
    classify_mod: Any,
) -> None:
    absent_404 = int(entry.get("absent_404_count", 0) or 0)
    if classify_mod.local_state(local) is classify_mod.LocalState.ABSENT:
        _append_local_gone(drift, local_id=local_id, jira_key=jira_key, absent_404=absent_404)
        return
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
    drift: dict[str, Any], *, local_by_id: dict[str, dict[str, Any]], bindings: dict[str, Any]
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
    drift: dict[str, Any],
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


def _finalize_binding_drift(drift: dict[str, Any]) -> dict[str, Any]:
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
) -> dict:
    """Binding-level drift audit (epic 3006-e198, child 8de5) — the REPORT consumer
    of the ONE convergence classifier (the live pass ACTS on the same Decisions).

    Reads ``.bridge_state/bindings.json`` + ``bindings-retired.json`` READ-ONLY
    (never writes a rebar-id label — L9 audit boundary) and runs ``classify()``
    over every binding and every unbound snapshot key, projecting the Decisions
    into a findings dict:

      * ``would_terminal`` — TERMINAL_TRANSITION: bound + local archived/deleted +
        Jira live (drift class A).
      * ``dangling`` — PROBE_GET: bound key absent from the Jira snapshot (drift
        class C candidate). Per ADR 0028 absence is not *proof* of deletion — a live
        probe confirms — so the report labels it a candidate, and the acting path
        (13eb) still requires a confirmed 404.
      * ``local_gone`` — ALERT: bound but the local ticket is absent from the store.
      * ``unbound_jira`` — ADOPT: a Jira-native issue in the snapshot with no
        binding (drift class B).
      * ``retired_overlap`` — a jira_key present in BOTH the live and retired stores.
      * ``indeterminate`` — a key-set snapshot proves a terminal local ticket's
        Jira issue is present, but does not carry the Jira status needed to decide
        whether a terminal transition is required.
    The Jira snapshot is taken from the persisted ``prev_snapshot.json`` artifact
    (no live fetch), so the whole audit is OFFLINE. Without a snapshot (none
    persisted, or ``use_prev_snapshot=False`` and none injected) only the
    local-decidable cells (local-archived ``would_terminal``, ``local_gone``) run.
    ``local_states`` / ``jira_snapshot`` are injectable seams for testing. This is
    the parity ORACLE the epic's convergence heals are validated against.
    """
    drift = _empty_binding_drift()
    bridge_state = tickets_tracker / ".bridge_state"
    store = _read_json(bridge_state / "bindings.json")
    if not isinstance(store, dict):
        # No store (or unreadable) → nothing bindings-level to audit.
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
        rmap = retired.get("retired")
        keys = set(rmap) if isinstance(rmap, (dict, list)) else set()
        return key in keys

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
        assert jira_snapshot is not None  # narrowed by have_snapshot
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
            local=local,
            entry=entry,
            classify_mod=classify_mod,
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

    # Overlap sanity: a key must not be both a live binding and retired.
    retired = _read_json(bridge_state / "bindings-retired.json")
    if isinstance(retired, dict):
        retired_map = retired.get("retired")
        retired_keys = set(retired_map) if isinstance(retired_map, (dict, list)) else set()
        live_keys = set(reverse) if isinstance(reverse, dict) else set()
        for key in sorted(retired_keys & live_keys):
            drift["retired_overlap"].append({"jira_key": key})

    return _finalize_binding_drift(drift)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _append_store_integrity_lines(lines: list[str], store_integrity: list[dict]) -> None:
    if not store_integrity:
        return
    lines.append("")
    lines.append("--- Binding Store Integrity ---")
    for entry in store_integrity:
        details = " ".join(f"{key}={value}" for key, value in entry.items() if key != "kind")
        lines.append(f"  {entry['kind']}: {details}")


def _append_alerting_drift_lines(lines: list[str], binding_drift: dict) -> None:
    drift_total = sum(len(binding_drift.get(k, [])) for k in _ALERTING_DRIFT_CLASSES)
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


def _informational_drift_present(binding_drift: dict) -> bool:
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


def _append_informational_drift_lines(lines: list[str], binding_drift: dict) -> None:
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


def _format_report(findings: dict) -> str:
    """Format the audit findings as a human-readable report."""
    unknown_types = findings.get("unknown_event_types", [])
    store_integrity = findings.get("store_integrity", [])
    binding_drift = findings.get("binding_drift") or {}
    drift_total = sum(len(binding_drift.get(k, [])) for k in _ALERTING_DRIFT_CLASSES)

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
    if not (store_integrity or drift_total or _informational_drift_present(binding_drift)):
        lines.append("")
        lines.append("No issues found.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 clean, 1 findings, or 2 operational failure."""
    # Canonical --output/-o flag via the single source of truth, then argparse the
    # rest. text -> human report; json -> the three-field audit contract.
    from rebar._engine_support.output import OutputFormatError, parse_output

    raw = list(sys.argv[1:]) if argv is None else list(argv)
    try:
        out_fmt, raw = parse_output(raw, "report")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(
        description="Audit committed bridge events and binding-store integrity offline.",
    )
    parser.add_argument(
        "--tickets-tracker",
        default=None,
        help=(
            "Path to the .tickets-tracker directory. "
            "Defaults to the REBAR_TRACKER_DIR env var "
            "or <repo-root>/.tickets-tracker."
        ),
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help=(
            "Prune reverse bindings that have no forward entry "
            "(store_integrity / reverse_missing_forward). Refuses if any other "
            "integrity kind is present. This is the only writing mode; the audit "
            "itself never writes."
        ),
    )
    parser.add_argument(
        "--live-visibility",
        action="store_true",
        help=(
            "Opt-in: additionally run a READ-ONLY, ADVISORY live check that the mapped "
            "project keys + legacy_default are visible to the bridge bot, reusing the "
            "reconcile-pass visibility helper. Requires live Jira credentials "
            "(JIRA_URL / JIRA_USER / JIRA_API_TOKEN); when absent it skips cleanly. The "
            "advisory is written to stderr and never changes the exit code."
        ),
    )
    args = parser.parse_args(raw)

    # Resolve tracker path: explicit arg > the full config resolver (env override >
    # the ``tracker.dir`` key > the default name under the detected repo root).
    if args.tickets_tracker:
        tracker_path = Path(args.tickets_tracker)
    else:
        # Fall back to repo root detection
        try:
            import subprocess

            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )
            repo_root = Path(result.stdout.strip())
        except Exception:  # noqa: BLE001 — git rev-parse fallback: an unresolvable repo root defaults to cwd
            repo_root = Path.cwd()
        # fsck walks the tracker directly by design, but the store is RELOCATABLE:
        # the previous branch honoured only the env override, so a store relocated by
        # the ``tracker.dir`` KEY was audited at the wrong path.
        from rebar.config import tracker_dir as _resolve_store

        tracker_path = _resolve_store(repo_root)

    # --repair is the ONE writing mode, and it lives in its own module so the
    # audit functions above keep their read-only (L9) boundary. It consumes
    # audit_store_integrity() rather than re-deriving the orphan set.
    if args.repair:
        from rebar._commands.bridge_repair import prune_orphan_reverse_bindings

        return prune_orphan_reverse_bindings(tracker_path, argv=raw)

    try:
        findings = audit_bridge_mappings(tracker_path)
    except RebarError as exc:
        diagnostic = exc.stderr or str(exc)
        sys.stderr.write(f"Error: {diagnostic}\n")
        return 2
    if out_fmt == "json":
        print(js_safe_dumps(findings))
    else:
        print(_format_report(findings))

    # Opt-in live mapped-project visibility (ticket 9702): READ-ONLY + ADVISORY.
    # Rendered to STDERR so the pinned stdout JSON contract (bridge_fsck.schema.json)
    # is byte-identical to today, and it NEVER changes the exit code below.
    if args.live_visibility:
        from rebar._engine_support.bridge_fsck_visibility import (
            audit_mapped_project_visibility,
            format_visibility_advisory,
        )

        verdict = audit_mapped_project_visibility(tracker_path.parent, env=dict(os.environ))
        for line in format_visibility_advisory(verdict):
            print(line, file=sys.stderr)

    # unknown_event_types is an informational WARN (upgrade signal), never a bridge
    # "issue" — it must not change the exit code. Alertable binding_drift IS real
    # drift (the class-D blindness this child heals), so it DOES set a non-zero
    # exit — but only the offline-decidable, actionable classes. dangling and
    # absent_in_window_unprobed are informational (ADR 0028 §1; bug f436) and must
    # NOT gate the exit code, else windowed absence goes red forever, unhealable.
    binding_drift = findings.get("binding_drift") or {}
    drift_total = sum(len(binding_drift.get(k, [])) for k in _ALERTING_DRIFT_CLASSES)
    has_issues = bool(findings.get("store_integrity")) or drift_total
    return 1 if has_issues else 0


if __name__ == "__main__":
    sys.exit(main())
