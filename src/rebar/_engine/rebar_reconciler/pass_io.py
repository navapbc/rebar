#!/usr/bin/env python3
"""Local-side persistence for a reconciliation pass.

Owns the filesystem artifacts a pass writes and the lazy loaders for the
sibling stores it consults:
  * the local↔Jira id/field mapping (``mapping.json``) — load + atomic writes +
    set-field provenance,
  * the reschedule contract (``RescheduleError``/``EXIT_RESCHEDULE``) raised when
    the tickets-branch write exhausts ``rebase_retry``,
  * lazy loaders for ``conflict_resolver`` and ``alert_store``.

This is the unit-of-work/persistence seam beneath the orchestrator: ``applier``
re-exports ``RescheduleError``/``EXIT_RESCHEDULE`` so ``__main__``'s
``getattr(applier, …)`` and the public error contract keep resolving.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Exit code signalling that the caller should reschedule this pass.
# Distinct from 1 (error) and 0 (success).  Chosen to be outside the
# range used by common POSIX utilities so it remains unambiguous.
EXIT_RESCHEDULE: int = 75


class RescheduleError(Exception):
    """Raised by apply() when rebase_retry exhausts all write attempts.

    Carries the attempt count and the last error message so the caller can
    emit a structured health event before exiting with EXIT_RESCHEDULE.
    No retry-counter file is written to disk; the next pass starts fresh.
    """

    def __init__(self, attempt_count: int, last_error: str) -> None:
        super().__init__(f"reject_and_reschedule after {attempt_count} attempt(s): {last_error}")
        self.attempt_count = attempt_count
        self.last_error = last_error


def _load_conflict_resolver():
    """Load conflict_resolver module via importlib."""
    resolver_path = Path(__file__).parent / "conflict_resolver.py"
    spec = importlib.util.spec_from_file_location(
        "rebar_reconciler_conflict_resolver", resolver_path
    )
    if spec is None:
        raise FileNotFoundError(f"conflict_resolver.py not found at {resolver_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("rebar_reconciler_conflict_resolver", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_alert_store():
    """Load the sibling alert_store module (mirrors invariants._load_alert_store).

    Used by the outbound batch loop to record soft-fail alerts (e.g.,
    AssigneeNotFoundError per bug 17b5) without aborting the pass.
    """
    alert_path = Path(__file__).parent / "alert_store.py"
    spec = importlib.util.spec_from_file_location("rebar_reconciler_alert_store", alert_path)
    if spec is None:
        raise FileNotFoundError(f"alert_store.py not found at {alert_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("rebar_reconciler_alert_store", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def record_parent_divergence(
    kind: str,
    key: str | None,
    local_id: str | None,
    parent: str | None,
    exc: BaseException,
    repo_root: Path | None = None,
) -> None:
    """Record that an outbound parent did NOT reach the tracker (ticket 39c1 AC4).

    The dispatcher keeps warn-and-continue — a parent failure must not abort the
    rest of the batch — but a log line is not a signal: the pass still exits 0 and
    nothing durable says the hierarchy diverged. This writes the same
    ``bridge_alerts`` record that ``apply_handlers.record_backstop_failure`` uses
    for the identical shape of non-fatal-but-real failure, so the divergence shows
    up wherever alerts already surface instead of only in a log nobody reads.

    ``kind`` discriminates the operator's next move and the two are not
    interchangeable: ``outbound-parent-unrepresentable`` means this deployment
    cannot hold the relationship at all (no retry helps), while
    ``outbound-parent-rejected`` means Jira refused THIS parent on hierarchy
    grounds and a different one would be accepted.

    Fail-open by construction. Observability must never cost delivery, so an
    unwritable state directory degrades to a warning rather than failing a
    mutation that would otherwise land.
    """
    try:
        if repo_root is None:
            from rebar.config import repo_root as _owned_repo_root

            repo_root = _owned_repo_root()
        _load_alert_store().append(
            {
                "kind": kind,
                "key": key,
                "local_id": local_id,
                "parent": parent,
                "timestamp_ns": time.time_ns(),
                "reason": str(exc),
            },
            repo_root=repo_root,
        )
    except Exception as alert_exc:  # noqa: BLE001 — alerting is best-effort; never fail a pass
        logger.warning("could not record %s alert for %s: %r", kind, key, alert_exc)


# Capability-absent alerts recur for the LIFE of a non-capable backend — a Data Center
# transport that implements neither capability Protocol would otherwise append one record
# per sub-op per pass, unbounded. The gap is a property of the (capability, site) pair, not
# of the individual sub-op, so record it at most ONCE per process per pair. Losing the
# per-sub-op multiplicity costs nothing: the operator's next move is identical whether one
# comment or two hundred were skipped for the same missing capability.
_CAPABILITY_GAPS_SEEN: set[tuple[str, str]] = set()


def record_capability_gap(
    capability: str,
    member: str,
    site: str,
    key: str | None,
    local_id: str | None = None,
    repo_root: Path | None = None,
) -> bool:
    """Record that a DESIGNED capability skip happened (ticket a3fa).

    This is the "capability absent" half of the skip-vs-failure split. It writes the SAME
    ``bridge_alerts`` record ``record_parent_divergence`` uses for the parent-divergence
    class, under a DISTINCT ``kind`` (``outbound-<capability>-capability-absent``), so a
    skip surfaces wherever alerts already surface and is never mistaken for the
    ``outbound-*-failed`` kinds a real exception produces.

    Deduped per process per ``(capability, site)`` — see ``_CAPABILITY_GAPS_SEEN``. Returns
    True when this call wrote the record, False when it was deduped, so callers can log the
    INFO line on the same edge.

    Fail-open by construction, exactly like ``record_parent_divergence``: an unwritable
    state directory degrades to a warning rather than failing a mutation that would land.
    """
    kind = f"outbound-{capability}-capability-absent"
    first = (capability, site) not in _CAPABILITY_GAPS_SEEN
    _CAPABILITY_GAPS_SEEN.add((capability, site))
    if not first:
        return False
    try:
        if repo_root is None:
            from rebar.config import repo_root as _owned_repo_root

            repo_root = _owned_repo_root()
        _load_alert_store().append(
            {
                "kind": kind,
                "key": key,
                "local_id": local_id,
                "capability": capability,
                "member": member,
                "site": site,
                "timestamp_ns": time.time_ns(),
                "reason": (
                    f"transport does not implement {capability}; "
                    f"{member} skipped at {site} (designed skip, not a failure)"
                ),
            },
            repo_root=repo_root,
        )
    except Exception as alert_exc:  # noqa: BLE001 — alerting is best-effort; never fail a pass
        logger.warning("could not record %s alert for %s: %r", kind, key, alert_exc)
    return True


def _load_mapping(mapping_path: Path) -> dict:
    """Load mapping.json, returning an empty dict if missing or corrupt.

    F10: when the file parses but contains a non-dict (e.g. a list or string
    from a corrupt write), downstream code that calls ``data[jira_key] = ...``
    would raise TypeError. Guard by returning ``{}`` for any non-dict value;
    subsequent writes will overwrite the corrupt file with a clean dict.
    """
    if mapping_path.exists():
        try:
            data = json.loads(mapping_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data
    return {}


def _write_mapping_json_atomic(mapping_path: Path, data: dict) -> None:
    """Write data to mapping_path atomically using temp-file + os.replace.

    Args:
        mapping_path: Full path to mapping.json.
        data:         Complete dict to serialize.
    """
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=mapping_path.parent, suffix=".tmp", prefix="mapping_")
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp_path, mapping_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass  # cleanup is best-effort; preserve and re-raise original write error
        raise


def _persist_field_provenance(
    mapping_path: Path,
    jira_key: str,
    field_name: str,
    field_value,
) -> None:
    """Persist field provenance for a set-valued field to mapping.json.

    Reads the current mapping.json, updates
    ``mapping[jira_key]["field_provenance"][field_name]`` with a provenance_record
    list derived from field_value, then writes back atomically.

    Args:
        mapping_path: Full path to mapping.json.
        jira_key:     Jira issue key (top-level key in mapping).
        field_name:   Name of the set-valued field (e.g., "labels").
        field_value:  The field value (list) from the mutation.
    """
    # Build the provenance_record list from the field value
    if isinstance(field_value, list):
        provenance_record = list(field_value)
    elif field_value is not None:
        provenance_record = [field_value]
    else:
        provenance_record = []

    data = _load_mapping(mapping_path)

    # Ensure nested structure exists
    if jira_key not in data:
        data[jira_key] = {}
    if not isinstance(data[jira_key], dict):
        data[jira_key] = {}
    if "field_provenance" not in data[jira_key]:
        data[jira_key]["field_provenance"] = {}

    data[jira_key]["field_provenance"][field_name] = provenance_record

    _write_mapping_json_atomic(mapping_path, data)


def _write_mapping_atomic(mapping_path: Path, local_id: str, jira_key: str) -> None:
    """Atomically update mapping.json with local_id -> jira_key entry.

    Uses a temp-file + os.replace pattern so readers never see a partial write.

    Args:
        mapping_path: Full path to mapping.json.
        local_id:     Local ticket ID (key to set).
        jira_key:     Jira issue key (value to set).
    """
    mapping_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing mapping (tolerate missing file)
    existing = _load_mapping(mapping_path)
    existing[local_id] = jira_key

    _write_mapping_json_atomic(mapping_path, existing)


def _handle_failed_write_result(write_result, pass_id: str) -> None:
    """Emit a health event to stderr and raise RescheduleError for a failed write.

    Called when rebase_retry returns ok=False.  The only kind that maps to a
    reschedule exit is 'reject_and_reschedule'; other kinds propagate as-is
    through other code paths (HeadDriftError for drift, exception for error).

    Args:
        write_result: Result(ok=False) returned by rebase_retry.
        pass_id:      Current reconciliation pass identifier (included in the
                      health event for traceability).

    Raises:
        RescheduleError: Always, when this function is called.
    """
    event = write_result.event
    attempt_count = event.attempt if event is not None else 0
    last_error = event.message if event is not None else ""

    health_event = {
        "kind": "reject_and_reschedule",
        "pass_id": pass_id,
        "attempt_count": attempt_count,
        "last_error": last_error,
    }
    print(json.dumps(health_event), file=sys.stderr)
    raise RescheduleError(attempt_count=attempt_count, last_error=last_error)
