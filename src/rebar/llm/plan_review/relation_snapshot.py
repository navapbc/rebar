"""Canonical ticket-relation material captured before plan-review signing.

This module deliberately stays below the signing and orchestration layers.  It
reduces the ticket store once, normalizes the plan's direct material relations,
and returns the clean tracker revision that the later atomic-signing work uses
as its optimistic-concurrency token.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from rebar import config
from rebar._engine_support import reads as ticket_reads
from rebar.reducer import reduce_all_tickets

PlanMaterialRole = Literal["child", "prerequisite"]
PlanRelationSnapshotReason = Literal[
    "missing-target",
    "ambiguous-reference",
    "malformed-reference",
    "reducer-error",
    "canonical-id-mismatch",
    "store-read-failure",
]

_NATIVE_CANONICAL_ID_RE = re.compile(r"^[a-z0-9]{4}(?:-[a-z0-9]{4}){3}$")
_JIRA_CANONICAL_ID_RE = re.compile(r"^jira-[a-z][a-z0-9]+-[0-9]+$")
_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")


def is_canonical_ticket_id(value: object) -> bool:
    """Whether ``value`` is a canonical on-disk local ticket-directory ID.

    Native tickets use the historical four-by-four form. Jira-originated
    tickets deterministically use ``jira-<lowercase-project>-<issue-number>``.
    Aliases and prefixes are deliberately excluded from this storage grammar.
    """

    text = str(value) if isinstance(value, str) else ""
    return bool(_NATIVE_CANONICAL_ID_RE.fullmatch(text) or _JIRA_CANONICAL_ID_RE.fullmatch(text))


@dataclass(frozen=True, order=True)
class PlanMaterialPin:
    role: PlanMaterialRole
    canonical_id: str
    material_fingerprint: str


@dataclass(frozen=True)
class PlanRelationSnapshot:
    subject_state: dict
    ticket_states_by_id: dict[str, dict]
    child_ids: tuple[str, ...]
    prerequisite_ids: tuple[str, ...]
    related_material: tuple[PlanMaterialPin, ...]
    ticket_store_revision: str


class PlanRelationSnapshotError(RuntimeError):
    """A closed, stable failure contract for relation-snapshot collection."""

    REASONS = frozenset(
        {
            "missing-target",
            "ambiguous-reference",
            "malformed-reference",
            "reducer-error",
            "canonical-id-mismatch",
            "store-read-failure",
        }
    )

    def __init__(
        self,
        reason: PlanRelationSnapshotReason,
        *,
        canonical_id: str | None = None,
        reference: str | None = None,
    ) -> None:
        if reason not in self.REASONS:
            raise ValueError(f"unknown plan relation snapshot reason: {reason}")
        self.reason = reason
        self.canonical_id = canonical_id
        self.reference = reference
        super().__init__(reason)


def _store_error(tracker: str | os.PathLike[str]) -> PlanRelationSnapshotError:
    return PlanRelationSnapshotError("store-read-failure", reference=str(tracker))


def tracker_head_sha(tracker: str | os.PathLike[str], *, ignore_untracked: bool = False) -> str:
    """Return a clean tickets-tracker HEAD, or fail with one stable reason.

    Freshness is established before all three strict git reads.  Dirty worktree,
    index-conflict, process, path, IO, and malformed-output failures intentionally
    collapse to ``store-read-failure``; callers must never interpret a best-effort
    or ``unknown`` revision as a safe signing token.
    """

    tracker_text = str(tracker)
    try:
        # Validate the raw value BEFORE Path normalization or freshness.  In
        # particular, ``Path("")`` means the ambient current directory; allowing
        # that through would let ensure_fresh create lock/throttle artifacts in
        # an unrelated repository.  A tracker is a non-empty git worktree (its
        # ``.git`` may be a file for a linked worktree or a directory).
        if not tracker_text.strip():
            raise _store_error(tracker_text)
        tracker_path = Path(tracker_text)
        if not tracker_path.is_dir() or not (tracker_path / ".git").exists():
            raise _store_error(tracker_text)

        ticket_reads.ensure_fresh(tracker_text)

        def run(*args: str) -> str:
            proc = subprocess.run(
                ["git", "-C", tracker_text, *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return proc.stdout or ""

        status_args = (
            ("status", "--porcelain", "--untracked-files=no")
            if ignore_untracked
            else (
                "status",
                "--porcelain",
            )
        )
        if run(*status_args):
            raise _store_error(tracker_text)
        if run("ls-files", "-u"):
            raise _store_error(tracker_text)
        head = run("rev-parse", "HEAD").strip()
        if not _HEAD_RE.fullmatch(head):
            raise _store_error(tracker_text)
        return head
    except PlanRelationSnapshotError:
        raise
    except Exception:  # noqa: BLE001 — every strict tracker read failure has one contract
        raise _store_error(tracker_text) from None


def _valid_reference(reference: object) -> bool:
    if not isinstance(reference, str) or not reference or reference.strip() != reference:
        return False
    if reference in (".", "..") or reference.startswith(".") or "\x00" in reference:
        return False
    return not any(sep and sep in reference for sep in ("/", "\\", os.sep, os.altsep))


def _resolve_reference(
    reference: object,
    states: dict[str, dict],
    aliases: dict[str, set[str]],
) -> str:
    if not _valid_reference(reference):
        raise PlanRelationSnapshotError("malformed-reference", reference=str(reference))
    ref = str(reference)
    if ref in states:
        return ref

    matches = set(aliases.get(ref, set()))
    # Canonical prefix and historical 8-character/8-hex-shaped forms share the
    # same unambiguous prefix rule.  Aliases win only when they are themselves
    # unique; a collision across forms is ambiguous rather than order-dependent.
    if len(ref) >= 4:
        matches.update(ticket_id for ticket_id in states if ticket_id.startswith(ref))
    if len(matches) > 1:
        raise PlanRelationSnapshotError("ambiguous-reference", reference=ref)
    if not matches:
        raise PlanRelationSnapshotError("missing-target", reference=ref)
    return next(iter(matches))


def _load_states(tracker: Path) -> tuple[dict[str, dict], dict[str, set[str]]]:
    try:
        entries = sorted(
            entry.name
            for entry in tracker.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )
    except (OSError, ValueError, TypeError):
        raise _store_error(tracker) from None
    try:
        reduced = reduce_all_tickets(
            tracker,
            exclude_archived=False,
            exclude_deleted=False,
            exclude_session_logs=False,
        )
    except Exception:  # noqa: BLE001 — a failed store reduction is a stable reducer error
        raise PlanRelationSnapshotError("reducer-error") from None
    if len(entries) != len(reduced):
        raise _store_error(tracker)

    states: dict[str, dict] = {}
    aliases: dict[str, set[str]] = {}
    for directory_id, state in zip(entries, reduced, strict=True):
        if not isinstance(state, dict) or state.get("status") in ("error", "fsck_needed"):
            raise PlanRelationSnapshotError("reducer-error", reference=directory_id)
        canonical_id = state.get("ticket_id")
        if canonical_id != directory_id or not is_canonical_ticket_id(canonical_id):
            raise PlanRelationSnapshotError(
                "canonical-id-mismatch",
                canonical_id=str(canonical_id) if canonical_id is not None else None,
                reference=directory_id,
            )
        states[directory_id] = state
        alias = state.get("alias")
        if isinstance(alias, str) and alias:
            aliases.setdefault(alias, set()).add(directory_id)
    return states, aliases


def _context_for(
    ticket_id: str,
    states: dict[str, dict],
    aliases: dict[str, set[str]],
) -> Any:
    from .det_floor import PlanContext

    state = states[ticket_id]
    children: list[dict] = []
    for candidate_id, candidate in states.items():
        parent = candidate.get("parent_id")
        if not parent:
            continue
        try:
            canonical_parent = _resolve_reference(parent, states, aliases)
        except PlanRelationSnapshotError:
            continue
        if canonical_parent == ticket_id and candidate.get("status") != "deleted":
            children.append({"ticket_id": candidate_id})
    return PlanContext(
        ticket_id=ticket_id,
        ticket_type=state.get("ticket_type", ""),
        title=state.get("title", ""),
        description=state.get("description", ""),
        state=state,
        children=children,
    )


def collect_plan_relation_snapshot(
    ticket_id: str, *, repo_root=None, ignore_untracked: bool = False
) -> PlanRelationSnapshot:
    """Collect canonical direct-child/prerequisite material in one store reduction."""

    tracker = Path(config.tracker_dir(repo_root))
    revision = (
        tracker_head_sha(tracker, ignore_untracked=True)
        if ignore_untracked
        else tracker_head_sha(tracker)
    )
    states, aliases = _load_states(tracker)
    subject_id = _resolve_reference(ticket_id, states, aliases)
    subject = states[subject_id]
    if subject.get("status") == "deleted":
        raise PlanRelationSnapshotError(
            "missing-target", canonical_id=subject_id, reference=ticket_id
        )

    child_ids: set[str] = set()
    prerequisite_ids: set[str] = set()
    for candidate_id, candidate in states.items():
        parent = candidate.get("parent_id")
        if parent:
            try:
                canonical_parent = _resolve_reference(parent, states, aliases)
            except PlanRelationSnapshotError:
                canonical_parent = None
            if canonical_parent == subject_id and candidate.get("status") != "deleted":
                child_ids.add(candidate_id)

        for dep in candidate.get("deps") or []:
            if not isinstance(dep, dict):
                continue
            relation = dep.get("relation")
            reference = dep.get("target_id", dep.get("target"))
            if relation == "depends_on" and candidate_id == subject_id:
                prerequisite_ids.add(_resolve_reference(reference, states, aliases))
            elif relation == "blocks":
                target_id = _resolve_reference(reference, states, aliases)
                if target_id == subject_id:
                    prerequisite_ids.add(candidate_id)

    for target_id in sorted(child_ids | prerequisite_ids):
        if target_id not in states or states[target_id].get("status") == "deleted":
            raise PlanRelationSnapshotError(
                "missing-target", canonical_id=target_id, reference=target_id
            )

    pins: list[PlanMaterialPin] = []
    # Keep ordinary manifest/claim-gate imports free of the optional runner
    # stack; relation collection itself runs at the LLM-operation boundary.
    from .pass1 import material_fingerprint

    for role, ids in (("child", child_ids), ("prerequisite", prerequisite_ids)):
        for target_id in sorted(ids):
            try:
                fingerprint = material_fingerprint(_context_for(target_id, states, aliases))
            except PlanRelationSnapshotError:
                raise
            except Exception:  # noqa: BLE001 — malformed reduced material fails closed
                raise PlanRelationSnapshotError(
                    "reducer-error", canonical_id=target_id, reference=target_id
                ) from None
            pins.append(
                PlanMaterialPin(
                    cast(PlanMaterialRole, role),
                    target_id,
                    fingerprint,
                )
            )

    return PlanRelationSnapshot(
        subject_state=subject,
        ticket_states_by_id=states,
        child_ids=tuple(sorted(child_ids)),
        prerequisite_ids=tuple(sorted(prerequisite_ids)),
        related_material=tuple(sorted(pins)),
        ticket_store_revision=revision,
    )
