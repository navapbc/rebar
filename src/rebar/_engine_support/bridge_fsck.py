"""Bridge-specific fsck audit (in-process; canonical home).

Audits bridge state without a Jira client. Unknown event types come from the
committed tickets ref without a checkout; binding integrity and drift inspect
the materialized tracker state:
  - Unknown top-level event types on the committed tickets ref
  - Forward/reverse binding-index integrity
  - Existing offline binding-drift classifications

Reached in-process via ``rebar.bridge_fsck()`` and the ``rebar bridge fsck`` CLI
arm. ``main()``
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

from rebar._engine_support.bridge_fsck_drift import (
    _ALERTING_DRIFT_CLASSES,
    _empty_binding_drift,
    _format_report,
    audit_binding_drift,
)
from rebar._errors import RebarError
from rebar._mcp_errors import js_safe_dumps
from rebar._store.gitutil import run_git

# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------
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
