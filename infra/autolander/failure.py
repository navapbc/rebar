"""Serial auto-lander failure handling (epic f1fa / S3): the two failure modes — CI-fail
(post-rebase `Verified -1`) and rebase-conflict (`POST /rebase` 409) — plus the
reconstruct-from-Gerrit outcome cache.

Invariants (see the S3 ticket):
- NEVER partial-land; NEVER evict a change from within a stack. On failure the WHOLE stack
  is handed back to its owning agent (remove `Autosubmit` from EVERY member).
- Gerrit is the source of truth. Only `needs_rebase` is PERSISTED (a self-invalidating marker
  keyed by change_id + patchset SHA); `merged`/`ci_failed`/`review_failed` are derived LIVE.
- Idempotent under retry/restart: record the outcome BEFORE label removal; label removal
  treats 404/409 as no-ops; an unacknowledged marker re-drives removal to completion.

STDLIB-ONLY except an allowed `import rebar` for ticket ops (mirrors loop.py); gerrit.py stays
stdlib-only.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from autolander.gerrit import GerritClient, GerritError
from autolander.loop import MARKER_HANDBACK, WipChain, emit_marker, hand_back  # noqa: F401

RECHECK_COMMENT = "recheck"  # ChatOps trigger (gerrit_to_platform: recheck = verify)
OUTCOME_CI_FAILED = "ci_failed"
OUTCOME_NEEDS_REBASE = "needs_rebase"
OUTCOME_VERIFIED = "verified"
TRANSITION_LOG = "transitions.log"  # rolling per-transition debug log (append-only JSONL)


@dataclass
class NeedsRebaseMarker:
    """The ONLY persisted outcome (a reconstruct-from-Gerrit cache entry). Self-invalidating:
    ignored once the change's current patchset SHA no longer matches `patchset_sha`."""

    change_id: str
    patchset_sha: str
    stack_id: str
    change_ids: list[str] = field(default_factory=list)
    tested_sha: str | None = None
    failing_change_id: str | None = None
    recorded_at: str | None = None
    acknowledged: bool = False


class MarkerStore:
    """A tiny JSON-file-backed store for `needs_rebase` markers under the auto-lander state
    dir (project-infra state, NOT a rebar event). One slot per `change_id` (a re-run for the
    same change_id+patchset_sha overwrites)."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sanitize(change_id: str) -> str:
        """Map a change_id (which may contain `/` and `~`) to a safe filename stem."""
        return str(change_id).replace("/", "_").replace("~", "_")

    def _slot(self, change_id: str) -> Path:
        return self._root / f"{self._sanitize(change_id)}.json"

    def _load(self, change_id: str) -> NeedsRebaseMarker | None:
        """Read the `change_id` slot, or None when absent."""
        slot = self._slot(change_id)
        if not slot.exists():
            return None
        data = json.loads(slot.read_text(encoding="utf-8"))
        return NeedsRebaseMarker(**data)

    def upsert(self, marker: NeedsRebaseMarker) -> None:
        """Write `marker` to its `change_id` slot (overwrite)."""
        slot = self._slot(marker.change_id)
        slot.write_text(json.dumps(asdict(marker)), encoding="utf-8")

    def get_valid(self, client: GerritClient, change_id: str) -> NeedsRebaseMarker | None:
        """Return the stored marker for `change_id` ONLY if its `patchset_sha` still matches
        the change's current patchset in Gerrit (self-invalidating); else None (and the stale
        marker is dropped — a new patchset means the rebase was resolved)."""
        marker = self._load(change_id)
        if marker is None:
            return None
        current = client.get_change(change_id, ["CURRENT_REVISION"]).get("current_revision")
        if marker.patchset_sha != current:
            return None
        return marker

    def acknowledge(self, change_id: str) -> None:
        """Mark the `change_id` slot's marker `acknowledged=true` (label-removal completed)."""
        marker = self._load(change_id)
        if marker is None:
            return
        marker.acknowledged = True
        self.upsert(marker)

    def unacknowledged(self) -> list[NeedsRebaseMarker]:
        """All markers with `acknowledged=false` (restart re-drives their removal)."""
        markers: list[NeedsRebaseMarker] = []
        for slot in sorted(self._root.glob("*.json")):
            data = json.loads(slot.read_text(encoding="utf-8"))
            marker = NeedsRebaseMarker(**data)
            if not marker.acknowledged:
                markers.append(marker)
        return markers

    def log_transition(self, event: str, **fields) -> None:
        """Append ONE rolling per-transition debug line (JSONL) to `transitions.log`. The
        authoritative outcome record is single-slot (self-invalidating, above); this rolling
        log is a debugging trail of every transition even though the record is overwritten."""
        at = fields.pop("at", None) or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = json.dumps({"event": event, "at": at, **fields}, sort_keys=True)
        with (self._root / TRANSITION_LOG).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def stack_owner_account(client: GerritClient, wip: WipChain) -> int | None:
    """The stack's owning agent = `owner._account_id` on the tip change."""
    return client.get_change(wip.change_id).get("owner", {}).get("_account_id")


def remove_autosubmit_from_stack(client: GerritClient, wip: WipChain) -> None:
    """Remove `Autosubmit` (vote 0) from EVERY member of the stack (never partial). Treats a
    per-member 404/409 as a no-op (idempotent under retry/restart)."""
    for member_id in wip.chain_member_ids:
        try:
            client.set_review(member_id, labels={"Autosubmit": 0})
        except GerritError as exc:
            if getattr(exc, "status", None) in (404, 409):
                continue  # already gone / conflict: idempotent no-op
            raise


def auto_recheck(
    client: GerritClient,
    wip: WipChain,
    *,
    await_terminal_verified,
) -> str:
    """CI-flake absorber: on a post-rebase `Verified -1`, post the `recheck` comment ONCE
    (sets `wip.rechecking=True` to guard against a re-post loop), then await the terminal
    Verified via the injected `await_terminal_verified(client, wip) -> int` (which treats the
    transient `Verified→0` from the clear-vote job as *recheck-running*, NOT a result).
    Returns `OUTCOME_VERIFIED` on a terminal `+1` (clears the flag) or `OUTCOME_CI_FAILED` on a
    terminal `-1`. Bounded to ONE recheck per patchset (the flag)."""
    if not wip.rechecking:
        client.set_review(wip.change_id, message=RECHECK_COMMENT)
        wip.rechecking = True
    vote = await_terminal_verified(client, wip)
    if vote >= 1:
        wip.rechecking = False
        return OUTCOME_VERIFIED
    return OUTCOME_CI_FAILED


def handle_ci_fail(client: GerritClient, wip: WipChain, store: MarkerStore | None = None) -> str:
    """After the auto-recheck still shows `Verified -1`: remove `Autosubmit` from ALL stack
    members and stop driving. Posts NO bot comment (the native `-1` + run logs are the
    signal). `ci_failed` is derived live (not persisted). Emits an `AUTOLANDER_HANDBACK`
    marker + a rolling per-transition log line. Returns `OUTCOME_CI_FAILED`."""
    emit_marker(MARKER_HANDBACK, {"change": wip.change_id, "reason": OUTCOME_CI_FAILED})
    if store is not None:
        store.log_transition(
            OUTCOME_CI_FAILED, change=wip.change_id, members=list(wip.chain_member_ids)
        )
    remove_autosubmit_from_stack(client, wip)
    return OUTCOME_CI_FAILED


def handle_rebase_conflict(
    client: GerritClient,
    wip: WipChain,
    store: MarkerStore,
    *,
    stack_id: str,
    tested_sha: str | None = None,
    now: str | None = None,
    failing_change_id: str | None = None,
) -> str:
    """On a `POST /rebase` 409: record a `needs_rebase` marker (acknowledged=false) BEFORE
    removing labels (idempotent ordering), remove `Autosubmit` from ALL members, then
    `acknowledge`. Never `allow_conflicts`; no conflict-marker patchset is created. Returns
    `OUTCOME_NEEDS_REBASE`."""
    tip_sha = client.get_change(wip.change_id).get("current_revision")
    marker = NeedsRebaseMarker(
        change_id=wip.change_id,
        patchset_sha=tip_sha,
        stack_id=stack_id,
        change_ids=list(wip.chain_member_ids),
        tested_sha=tested_sha,
        failing_change_id=failing_change_id,
        recorded_at=now,
        acknowledged=False,
    )
    store.upsert(marker)  # record BEFORE removal (idempotent ordering)
    store.log_transition(OUTCOME_NEEDS_REBASE, change=wip.change_id, stack_id=stack_id, at=now)
    emit_marker(
        MARKER_HANDBACK,
        {"change": wip.change_id, "stack_id": stack_id, "reason": OUTCOME_NEEDS_REBASE},
    )
    remove_autosubmit_from_stack(client, wip)
    store.acknowledge(wip.change_id)
    return OUTCOME_NEEDS_REBASE


def reconcile_on_restart(client: GerritClient, store: MarkerStore) -> list[str]:
    """On restart, re-drive every unacknowledged marker's `Autosubmit` removal to completion
    (idempotent), then acknowledge. Returns the change_ids re-driven."""
    re_driven: list[str] = []
    for marker in store.unacknowledged():
        wip = WipChain(
            change_id=marker.change_id,
            chain_member_ids=marker.change_ids or [marker.change_id],
        )
        remove_autosubmit_from_stack(client, wip)
        store.acknowledge(marker.change_id)
        re_driven.append(marker.change_id)
    return re_driven
