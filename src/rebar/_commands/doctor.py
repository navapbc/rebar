"""``doctor`` — find (and optionally repair) blocking edges that predate the
structural link rule.

Blocking-link comparability used to be decided by ticket TYPE TIER; ticket
7ab3-9df0-7a90-4ffd replaced that with a structural rule (endpoints must be
siblings; otherwise each escalates to a child of their nearest common ancestor).
A ``LINK`` event is durable and nothing re-resolves it on read, so edges written
under the old rule stay wrong on disk and keep distorting ``ready`` /
``next-batch`` / the parent-first claim cascade.

This module enumerates net-active blocking edges and asks the CURRENT resolver
what each one should be, so the audit can never drift from the rule it audits:

  * ``ancestor-blocking`` — the resolver reports the pair redundant, i.e. one
    endpoint is an ancestor of the other. Repair unlinks it; there is no correct
    replacement, because the hierarchy edge already expresses the relationship.
  * ``mis-escalated``     — the resolver returns a different pair than the one on
    disk. Repair replaces the edge with the resolved pair.
  * ``unreadable``        — the resolver could not read an endpoint. Reported,
    never repaired.

Repair writes the replacement BEFORE removing the stale edge: unlink-first would,
on any failure in between, destroy a dependency with nothing left to reconstruct
it from, whereas link-first fails toward a transient superset that the next scan
finishes. Repair is therefore resumable and idempotent, and no failure path loses
an edge.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rebar._commands._seam import CommandError, tracker_dir
from rebar._engine_support.output import OutputFormatError, parse_output
from rebar._store.gitutil import run_git
from rebar.graph._hierarchy import resolve_hierarchy_link
from rebar.graph._links import CyclicDependencyError, add_dependency
from rebar.graph._loader import reduce_all_tickets
from rebar.graph._relations import _BLOCKING_RELATIONS

# Force-written at the tracker's pre-run OID before the first repair write, so a
# whole run can be inspected against — or reset to — the state it started from.
# Mirrors fsck --repair's ``pre-a3-remediation`` (fsck_repair.py), including the
# ``-f``: a resumed or repeated repair re-points the tag at THAT run's starting
# state rather than failing because the tag already exists.
PRE_REPAIR_TAG = "pre-doctor-repair"

_KIND_ANCESTOR = "ancestor-blocking"
_KIND_MIS_ESCALATED = "mis-escalated"
_KIND_UNREADABLE = "unreadable"

# add_dependency raises CyclicDependencyError, the unlink path raises CommandError,
# and the guard/validation paths raise ValueError. CyclicDependencyError and
# CommandError both derive from Exception, NOT from ValueError, so catching
# ValueError alone would let a real write failure escape and abort the run
# mid-repair, leaving the store half-converted.
_REPAIR_FAULTS = (ValueError, CyclicDependencyError, CommandError)


def _blocking_edges(tracker: str) -> list[dict[str, str]]:
    """Return every net-active blocking edge in the store, source-first.

    Reuses the reducer's compiled ``deps[]`` — the same walk
    ``compute_archive_eligible`` uses — rather than re-deriving LINK/UNLINK replay
    and the SNAPSHOT fallback that ``graph._links._is_active_link`` already owns.
    """
    edges: list[dict[str, str]] = []
    for state in reduce_all_tickets(tracker, exclude_archived=False, exclude_session_logs=True):
        source = state.get("ticket_id")
        if not source:
            continue
        for dep in state.get("deps", []):
            relation = dep.get("relation")
            target = dep.get("target_id")
            if relation in _BLOCKING_RELATIONS and target:
                edges.append({"source": source, "target": target, "relation": relation})
    return edges


def _unlink_would_cancel(tracker: str, source: str, target: str) -> str:
    """Return the relation the next ``UNLINK`` on this ordered pair would cancel.

    ``UNLINK`` is pair-scoped by design — it carries no relation argument and
    cancels the MOST-RECENT net-active link for an ordered pair. So on a pair that
    also holds, say, a ``relates_to`` newer than its ``depends_on``, unlinking the
    blocking edge would silently remove the wrong relation. Callers compare this
    against the relation they intend to remove and decline on a mismatch, because
    the event model offers no relation-scoped unlink to reach for instead.
    """
    from pathlib import Path

    from rebar._commands.unlink import _get_link_info

    _uuid, relation = _get_link_info(Path(tracker) / source, target)
    return relation


def classify_edge(edge: dict[str, str], tracker: str) -> dict[str, Any] | None:
    """Classify one blocking edge against the CURRENT resolver, or None if clean."""
    source, target, relation = edge["source"], edge["target"], edge["relation"]
    result = resolve_hierarchy_link(source, target, tracker, relation)

    finding: dict[str, Any] = {
        "source": source,
        "target": target,
        "relation": relation,
    }

    if "error" in result:
        finding["kind"] = _KIND_UNREADABLE
        finding["detail"] = str(result["error"])
        return finding

    if result.get("is_redundant"):
        finding["kind"] = _KIND_ANCESTOR
        finding["detail"] = f"{source} and {target} are in an ancestor-descendant relationship"
        return finding

    resolved_source = str(result["resolved_source"])
    resolved_target = str(result["resolved_target"])
    if (resolved_source, resolved_target) != (source, target):
        finding["kind"] = _KIND_MIS_ESCALATED
        finding["resolved_source"] = resolved_source
        finding["resolved_target"] = resolved_target
        finding["detail"] = (
            f"recorded {source}→{target}, resolves to {resolved_source}→{resolved_target}"
        )
        return finding

    return None


def scan(tracker: str) -> list[dict[str, Any]]:
    """Return every finding in the store (read-only; writes nothing)."""
    findings = []
    for edge in _blocking_edges(tracker):
        finding = classify_edge(edge, tracker)
        if finding is not None:
            findings.append(finding)
    return findings


def _unlink_edge(source: str, target: str, tracker: str, *, repo_root=None) -> None:
    """Remove the pair's link via the shared UNLINK write path.

    Calls ``_write_unlink`` rather than ``unlink_core`` because the latter derives
    the tracker from ``repo_root``, while everything here threads an explicit
    tracker. This is the same event-writing path (and the same active-link
    validation) one level down.
    """
    from pathlib import Path

    from rebar._commands.unlink import _write_unlink

    _write_unlink(source, target, Path(tracker), repo_root=repo_root)


def repair_finding(finding: dict[str, Any], tracker: str, *, repo_root=None) -> dict[str, Any]:
    """Repair one finding in place, annotating it with ``repair_status``.

    Never raises for an unrepairable edge: the recorded edge is left exactly as it
    was, the reason is recorded, and the caller continues to the next finding.
    """
    kind = finding["kind"]
    source, target = finding["source"], finding["target"]

    if kind == _KIND_UNREADABLE:
        finding["repair_status"] = "unrepairable"
        finding["repair_reason"] = "unreadable-endpoint"
        return finding

    # UNLINK is pair-scoped, so confirm the link it would cancel is the blocking
    # one we mean to remove. On a mismatch another relation is newer on this pair
    # and would be destroyed instead — decline rather than delete the wrong edge.
    would_cancel = _unlink_would_cancel(tracker, source, target)
    if would_cancel != finding["relation"]:
        finding["repair_status"] = "unrepairable"
        finding["repair_reason"] = (
            f"ambiguous-pair: unlink would cancel {would_cancel or 'nothing'!r}, "
            f"not {finding['relation']!r}"
        )
        return finding

    try:
        if kind == _KIND_MIS_ESCALATED:
            # LINK BEFORE UNLINK — see the module docstring. A failure here leaves
            # the original edge untouched; a failure after leaves a superset the
            # next scan converges.
            add_dependency(
                finding["resolved_source"],
                finding["resolved_target"],
                tracker,
                relation=finding["relation"],
            )
        _unlink_edge(source, target, tracker, repo_root=repo_root)
    except _REPAIR_FAULTS as exc:
        finding["repair_status"] = "unrepairable"
        finding["repair_reason"] = f"{type(exc).__name__}: {exc}"
        return finding

    finding["repair_status"] = "repaired"
    return finding


def _pre_tag(tracker: str) -> str:
    """Force-write the rollback tag at the tracker's current OID; return that OID."""
    pre_oid = run_git(tracker, "rev-parse", "HEAD", check=False).stdout.strip()
    if pre_oid:
        run_git(tracker, "tag", "-f", PRE_REPAIR_TAG, pre_oid, check=False)
    return pre_oid


def _reconciler_in_flight(repo_root=None) -> bool:
    """Fail-closed probe for an in-flight reconciler pass (shared with fsck --repair)."""
    from rebar._commands.fsck_repair import _reconciler_in_flight as _probe

    return _probe(repo_root)


def run_repair(
    findings: list[dict[str, Any]], tracker: str, *, repo_root=None
) -> tuple[list[dict[str, Any]], str]:
    """Repair every finding under the write lock. Returns (findings, pre_oid)."""
    if _reconciler_in_flight(repo_root):
        raise CommandError(
            "Error: a reconciler pass is in flight (refs/reconciler/lock held or "
            "unreadable) — refusing to repair; retry once the pass completes"
        )

    # NO outer write lock. Every event write already takes the tracker write lock
    # for itself (append_event -> write_and_push -> stage_and_commit -> write_lock),
    # and that lock is NOT re-entrant: an outer hold made each inner acquisition
    # block until its 60s timeout and fail, so a repair pass completed nothing while
    # serialising the tracker for every other writer. Repair is resumable by design
    # (the replacement link is written before the stale unlink), so it needs no
    # cross-item atomicity — per-write locking is both correct and the only thing
    # that works.
    pre_oid = _pre_tag(tracker)
    for finding in findings:
        repair_finding(finding, tracker, repo_root=repo_root)

    return findings, pre_oid


def _print_text(findings: list[dict[str, Any]], pre_oid: str, *, repaired: bool) -> None:
    if pre_oid:
        print(f"doctor: pre-tag {PRE_REPAIR_TAG} @ {pre_oid[:12]}")
    for f in findings:
        status = f.get("repair_status")
        suffix = f" [{status}: {f.get('repair_reason', '')}]" if status else ""
        print(f"{f['kind']}: {f['source']} {f['relation']} {f['target']} — {f['detail']}{suffix}")
    outstanding = sum(1 for f in findings if f.get("repair_status") != "repaired")
    verb = "outstanding" if repaired else "finding(s)"
    print(f"doctor: {len(findings)} finding(s), {outstanding} {verb}")


def doctor_cli(argv: list[str], *, repo_root=None) -> int:
    """CLI entry: scan by default; ``--repair`` converts findings in place."""
    try:
        fmt, rest = parse_output(argv, allowed=("text", "json"), default="text")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    do_repair = "--repair" in rest
    dry_run = "--dry-run" in rest
    unknown = [a for a in rest if a not in ("--repair", "--dry-run")]
    if unknown:
        print(
            f"Usage: rebar doctor [--repair] [--dry-run] [--output json]\n"
            f"  unexpected argument(s): {' '.join(unknown)}",
            file=sys.stderr,
        )
        return 2

    tracker = str(tracker_dir(repo_root))
    findings = scan(tracker)

    pre_oid = ""
    if do_repair and not dry_run and findings:
        try:
            findings, pre_oid = run_repair(findings, tracker, repo_root=repo_root)
        except CommandError as exc:
            print(exc.message, file=sys.stderr)
            return 1

    if fmt == "json":
        print(
            json.dumps(
                {
                    "findings": findings,
                    "finding_count": len(findings),
                    "pre_repair_tag_oid": pre_oid,
                }
            )
        )
    else:
        _print_text(findings, pre_oid, repaired=do_repair and not dry_run)

    outstanding = sum(1 for f in findings if f.get("repair_status") != "repaired")
    return 1 if outstanding else 0
