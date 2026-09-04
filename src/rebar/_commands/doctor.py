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

Alongside the link audit it reports LOCK HEALTH — held/free, the ownership stamp,
holder liveness, hold age and staleness for each of the store's lock legs. That half
lives in :mod:`rebar._commands.doctor_locks` and is strictly read-only: a stale lock is
reported and advised on, never reclaimed. Only link findings are repairable, so lock
results never enter the repair loop below.

It also scans for the DIRTY-TRACKER wedge class fsck check 4.10 detects (ticket
c925-7669-ded8-43a3): tracked deletions of store artifacts restorable from HEAD,
untracked regenerable compaction leftovers, and orphaned ``.tmp-event-*`` staging
files. ``--repair`` heals the first two in TWO lock windows — the store write lock is
NON-reentrant, so the file mutations (restore via ``git checkout``; quarantine MOVE,
never delete) run under one short scoped ``write_lock`` window that is RELEASED before
``sync.reconverge`` (which takes the lock itself) converges the previously wedged
store. Before the first mutation a backup ref ``refs/rebar-doctor/<utc-ts>`` records
the tickets HEAD, mirroring tracker-maintenance's backup-ref envelope. Class 3 is
printed and never touched — a live ``.tmp-event-*`` belongs to an in-flight append.

Repair writes the replacement BEFORE removing the stale edge: unlink-first would,
on any failure in between, destroy a dependency with nothing left to reconstruct
it from, whereas link-first fails toward a transient superset that the next scan
finishes. Repair is therefore resumable and idempotent, and no failure path loses
an edge.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

from rebar._commands import (
    doctor_build_freshness,
    doctor_locks,
    doctor_mapping,
    doctor_mcp_client,
)
from rebar._commands._repair_pause import owned_repair_pause
from rebar._commands._seam import CommandError, tracker_dir
from rebar._engine_support.output import OutputFormatError, parse_output
from rebar._mcp_errors import js_safe_dumps
from rebar._store import lock as _store_lock
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

_KIND_DIRTY_DELETION = "tracker-dirty-deletion"
_KIND_DIRTY_LEFTOVER = "tracker-dirty-leftover"
_KIND_DIRTY_TMP_EVENT = "tracker-dirty-tmp-event"
_DIRTY_KINDS = {_KIND_DIRTY_DELETION, _KIND_DIRTY_LEFTOVER, _KIND_DIRTY_TMP_EVENT}
_MUTATING_DIRTY_KINDS = {_KIND_DIRTY_DELETION, _KIND_DIRTY_LEFTOVER}

# The rollback point for the dirty-tracker repairs: recorded at the tickets HEAD BEFORE
# the first mutation, mirroring tracker-maintenance's refs/rebar-maintenance/<utc>
# envelope (backup ref + audited actions; tracker-maintenance holds no write lock — its
# envelope, not a lock, is the precedent adopted here).
DOCTOR_BACKUP_REF_PREFIX = "refs/rebar-doctor/"

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


# (classes key, finding kind, detail). Keys match fsck's dirty_tracker_classes — THE
# single classifier — so doctor repairs exactly the set fsck reports (the same one-rule
# discipline tracker-maintenance applies to foreign_store_path_list).
_DIRTY_FINDING_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        "deletions",
        _KIND_DIRTY_DELETION,
        "tracked store file(s) deleted in the working tree; bytes intact at HEAD",
    ),
    (
        "leftovers",
        _KIND_DIRTY_LEFTOVER,
        "untracked regenerable compaction leftover(s)",
    ),
    (
        "tmp_events",
        _KIND_DIRTY_TMP_EVENT,
        "orphaned .tmp-event-* staging file(s) — never auto-touched; triage manually",
    ),
)


def scan_dirty(tracker: str) -> list[dict[str, Any]]:
    """The dirty-tracker wedge findings (read-only), one per non-empty class."""
    from rebar._commands.fsck_tracker_health import dirty_tracker_classes

    classes = dirty_tracker_classes(tracker)
    return [
        {"kind": kind, "paths": classes[key], "detail": detail}
        for key, kind, detail in _DIRTY_FINDING_SPECS
        if classes[key]
    ]


# raw-git-ok: doctor --repair backup-ref audit envelope (tracker-maintenance's pattern)
def _dirty_backup_ref(tracker: str) -> str | None:
    """Record ``refs/rebar-doctor/<utc-ts>`` at the tickets HEAD; None on failure.

    Must precede every dirty-tracker mutation — it is the whole-run rollback point
    (``git reset --hard <ref>`` restores the pre-repair commit; the quarantine holds
    any moved working-tree bytes)."""
    head = run_git(tracker, "rev-parse", "HEAD", check=False).stdout.strip()
    if not head:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    ref = f"{DOCTOR_BACKUP_REF_PREFIX}{stamp}"
    if run_git(tracker, "update-ref", ref, head, check=False).returncode != 0:
        return None
    return ref


def _quarantine_leftovers(tracker: str, paths: list[str]) -> bool:
    """Move (never delete) the paths into ``<git-common-dir>/reconverge-quarantine/
    <utc-ts>/`` — the same durable quarantine sync's merge recovery uses, via the same
    implementation so the two doors cannot drift. True only if every path moved."""
    from rebar._store.sync import _quarantine_untracked

    return _quarantine_untracked(tracker, paths)


def _reconverge(tracker: str) -> None:
    """Window 2 of the dirty repair: converge the previously wedged store. Takes the
    write lock ITSELF, which is why the caller must have released window 1 first."""
    from rebar._store import sync

    sync.reconverge(tracker)


# raw-git-ok: doctor --repair checkout-restore under the scoped write_lock; no event to append
def _apply_dirty_mutation(finding: dict[str, Any], tracker: str) -> None:
    """One file-mutating dirty repair, annotating ``repair_status`` in place.

    LOCK HELD BY THE CALLER (window 1). Restores are ``git checkout HEAD --`` (bytes
    exist at HEAD — non-destructive); leftovers are MOVED into quarantine, never
    deleted, so no repair path can lose event bytes."""
    if finding["kind"] == _KIND_DIRTY_DELETION:
        cp = run_git(tracker, "checkout", "HEAD", "--", *finding["paths"], check=False)
        ok = cp.returncode == 0
        reason = (cp.stderr or "").strip() or "git checkout failed"
    else:
        ok = _quarantine_leftovers(tracker, finding["paths"])
        reason = "quarantine move failed (missing path or unresolvable git common dir)"
    finding["repair_status"] = "repaired" if ok else "unrepairable"
    if not ok:
        finding["repair_reason"] = reason


def _repair_dirty(findings: list[dict[str, Any]], tracker: str) -> None:
    """Heal the dirty-tracker findings in TWO lock windows.

    The store write lock is NON-reentrant and ``sync.reconverge`` acquires it
    internally, so one outer hold across everything would deadlock window 2 exactly the
    way an outer hold starved the link repairs (see ``run_repair``). Window 1 takes ONE
    short scoped ``write_lock`` for all file mutations, then RELEASES it; window 2 lets
    reconverge self-lock. Before the first mutation the backup ref is recorded —
    no backup ref, no mutation.

    Class 3 (``.tmp-event-*``) is marked ``manual`` and never touched: a live staging
    file belongs to an in-flight append, and a dead one is named for human triage."""
    for finding in findings:
        if finding["kind"] == _KIND_DIRTY_TMP_EVENT:
            finding["repair_status"] = "manual"
            finding["repair_reason"] = "orphaned .tmp-event-* files are never auto-touched"
    mutating = [f for f in findings if f["kind"] in _MUTATING_DIRTY_KINDS]
    if not mutating:
        return
    backup_ref = _dirty_backup_ref(tracker)
    if backup_ref is None:
        for finding in mutating:
            finding["repair_status"] = "unrepairable"
            finding["repair_reason"] = "could not record the backup ref — refusing to mutate"
        return
    for finding in mutating:
        finding["backup_ref"] = backup_ref
    with _store_lock.write_lock(tracker):
        for finding in mutating:
            _apply_dirty_mutation(finding, tracker)
    # Lock RELEASED: reconverge takes it itself (non-reentrant), converging the
    # previously wedged store in the same command.
    _reconverge(tracker)


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
    with owned_repair_pause("doctor", repo_root, in_flight_probe=_reconciler_in_flight):
        # NO outer write lock. Every event write already takes the tracker write lock
        # for itself (append_event -> write_and_push -> stage_and_commit -> write_lock),
        # and that lock is NOT re-entrant: an outer hold made each inner acquisition
        # block until its 60s timeout and fail, so a repair pass completed nothing while
        # serialising the tracker for every other writer. Repair is resumable by design
        # (the replacement link is written before the stale unlink), so it needs no
        # cross-item atomicity — per-write locking is both correct and the only thing
        # that works.
        pre_oid = _pre_tag(tracker)
        # Dirty-tracker findings first: a wedged tree blocks the event writes the link
        # repairs below depend on. They manage their own two lock windows.
        dirty = [f for f in findings if f.get("kind") in _DIRTY_KINDS]
        if dirty:
            _repair_dirty(dirty, tracker)
        for finding in findings:
            if finding.get("kind") in _DIRTY_KINDS:
                continue
            repair_finding(finding, tracker, repo_root=repo_root)

    return findings, pre_oid


def _outstanding(findings: list[dict[str, Any]]) -> int:
    """Findings still needing action. ``manual`` (the class-3 triage notice) is a
    deliberate non-action, not a failed repair, so it does not fail the run."""
    return sum(1 for f in findings if f.get("repair_status") not in ("repaired", "manual"))


def _print_text(
    findings: list[dict[str, Any]],
    pre_oid: str,
    *,
    repaired: bool,
    lock_reports: list[dict[str, Any]],
    lock_faults: list[dict[str, Any]],
    mapping_findings: list[dict[str, Any]],
    mcp_client_findings: list[dict[str, Any]],
    build_freshness_findings: list[dict[str, Any]],
) -> None:
    if pre_oid:
        print(f"doctor: pre-tag {PRE_REPAIR_TAG} @ {pre_oid[:12]}")
    for ref in sorted({f["backup_ref"] for f in findings if f.get("backup_ref")}):
        print(f"doctor: backup ref {ref} (rollback: git reset --hard {ref})")
    for f in findings:
        status = f.get("repair_status")
        suffix = f" [{status}: {f.get('repair_reason', '')}]" if status else ""
        if "paths" in f:
            print(f"{f['kind']}: {len(f['paths'])} path(s) — {f['detail']}{suffix}")
            for path in f["paths"]:
                print(f"  {path}")
            continue
        print(f"{f['kind']}: {f['source']} {f['relation']} {f['target']} — {f['detail']}{suffix}")
    outstanding = _outstanding(findings)
    verb = "outstanding" if repaired else "finding(s)"
    print(f"doctor: {len(findings)} finding(s), {outstanding} {verb}")
    # The lock section keeps its own count: lock findings are never repairable, so folding
    # them into the line above would report a repair total that cannot be acted on.
    for line in doctor_locks.render_text(lock_reports, lock_faults):
        print(line)
    print(f"doctor: {len(lock_faults)} stale lock(s)")
    # Mapping findings render in their own section — like locks, they stay OUT of the
    # repair loop; each line carries the finding's detail (and its key where present).
    for f in mapping_findings:
        key = f.get("key")
        prefix = f"mapping[{key}]" if key else "mapping"
        print(f"{prefix} {f['severity']}: {f['detail']}")
    # MCP client-config findings render in their own section, and like locks and mapping
    # stay OUT of the repair loop — they describe the operator's client environment, not
    # the store, so there is nothing here for --repair to convert. Joined rather than
    # looped: render_text always emits its header line, so this never prints a blank.
    print("\n".join(doctor_mcp_client.render_text(mcp_client_findings)))
    # Build-freshness findings render in their own section on the same terms: read from
    # the host's updater state rather than the store, so outside the repair loop AND
    # outside the exit code. Joined rather than looped — render_text always emits its
    # header, so this never prints a blank line.
    print("\n".join(doctor_build_freshness.render_text(build_freshness_findings)))


def doctor_cli(argv: list[str], *, repo_root=None) -> int:
    """CLI entry: scan by default; ``--repair`` converts findings in place."""
    try:
        fmt, rest = parse_output(argv, allowed=("text", "json"), default="text")
    except OutputFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    do_repair = "--repair" in rest
    dry_run = "--dry-run" in rest
    from rebar._cli._parsers.core.repair import build_doctor

    # Parser of record for doctor's accepted grammar; the membership scan below is
    # retained because it owns the bespoke ``unexpected argument(s)`` reject/exit code.
    build_doctor(prog="rebar doctor").parse_known_args(rest)
    unknown = [a for a in rest if a not in ("--repair", "--dry-run")]
    if unknown:
        print(
            f"Usage: rebar doctor [--repair] [--dry-run] [--output json]\n"
            f"  unexpected argument(s): {' '.join(unknown)}",
            file=sys.stderr,
        )
        return 2

    tracker = str(tracker_dir(repo_root))
    # Dirty-tracker findings first: a wedged tree blocks event writes, so its repair
    # must precede the link repairs (run_repair preserves this ordering rule itself).
    findings = scan_dirty(tracker) + scan(tracker)
    # Read-only, and deliberately outside the repair path below: `run_repair` iterates
    # `findings`, so keeping lock results in their own list is what guarantees --repair
    # can never act on a lock (ticket metaphoric-fleeting-nutcracker). Sampled BEFORE any
    # repair for the same reason — a repair pass takes the write lock for each of its own
    # event writes, and a report gathered after that would describe doctor itself.
    lock_reports = doctor_locks.scan_locks(tracker)
    lock_faults = doctor_locks.lock_findings(lock_reports)

    # Mapping-config diagnostics: their OWN list, deliberately outside `findings` so
    # `run_repair` never iterates them (exactly as lock results stay out of the repair
    # loop). The config root is the repo root, not the tracker dir.
    mapping_findings = doctor_mapping.scan_mapping(repo_root)

    # MCP client-config diagnostics: pure, stdlib-only, and read from the operator's home
    # rather than the store, so they too stay outside `findings` / the repair loop.
    mcp_client_findings = doctor_mcp_client.scan_mcp_clients()

    # Is the rebar on this box current, and is whatever keeps it current still working?
    # Read from the host's main-tracking updater state on local disk — no network, no
    # alert sink — because the sink is exactly what went silent in bug ae97-a37b-9fa3-413a.
    build_freshness_findings = doctor_build_freshness.scan_build_freshness(repo_root=repo_root)

    pre_oid = ""
    if do_repair and not dry_run and findings:
        try:
            findings, pre_oid = run_repair(findings, tracker, repo_root=repo_root)
        except CommandError as exc:
            print(exc.message, file=sys.stderr)
            return 1

    if fmt == "json":
        print(
            js_safe_dumps(
                {
                    "findings": findings,
                    "finding_count": len(findings),
                    "pre_repair_tag_oid": pre_oid,
                    "locks": lock_reports,
                    "lock_findings": lock_faults,
                    "mapping_findings": mapping_findings,
                    "mcp_client_findings": mcp_client_findings,
                    "build_freshness_findings": build_freshness_findings,
                }
            )
        )
    else:
        _print_text(
            findings,
            pre_oid,
            repaired=do_repair and not dry_run,
            lock_reports=lock_reports,
            lock_faults=lock_faults,
            mapping_findings=mapping_findings,
            mcp_client_findings=mcp_client_findings,
            build_freshness_findings=build_freshness_findings,
        )

    outstanding = _outstanding(findings)
    # A stale lock is outstanding by the same rule as an unrepaired link finding: no live
    # process claims it. A HELD lock with a live holder is information, not a finding, so
    # it never fails a CI gate keyed on this exit code.
    blocking_mapping = doctor_mapping.has_blocking_mapping(mapping_findings)
    # MCP client findings are deliberately ADVISORY — they are read from the operator's
    # HOME, not from the store, so folding them into this exit code would make a
    # store-health gate depend on whichever client configs happen to sit on the box
    # running it. They are reported (text + JSON) and severity-classified via
    # doctor_mcp_client.has_blocking_mcp_client, which a caller that DOES want to gate on
    # client wiring can apply to `mcp_client_findings` itself.
    return 1 if outstanding or lock_faults or blocking_mapping else 0
