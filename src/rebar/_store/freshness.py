"""Is this clone's ticket store fresh enough for a GATE to certify against?

Bug ``cibophobic-moist-guineafowl`` (``b928-3ab6-5985-417b``). Under sustained
contention on ``origin/tickets`` a clone can spend hours neither publishing its own
writes nor adopting the remote's — the starvation root cause recorded on that ticket.
Every write still SIGNALS the condition (``push_status: final-push-rejected`` plus the
durable ``rebar-push-pending`` marker, operator decision B4 on ``vapoury-attack-lamb``),
and that contract is deliberately unchanged here: a write that cannot reach the shared
store still returns, never raises.

What went wrong is on the READ side, and it is a different kind of harm. On 2026-08-28 a
ticket carrying a valid ``completion-verifier`` attestation read as ``unsigned`` through
a clone that had not received the write. A gate that answers from such a store does not
merely answer LATE — it answers WRONG, and then mints (or withholds) an operation
certificate on the strength of it. A late comment is an inconvenience; a wrong
certification is a corrupted audit trail.

So the gate-critical read paths assert freshness before they decide. The alternative that
was considered and REJECTED is blanket write-refusal when the push is failing: that turns
a degraded-but-usable system into an outage — during the four-hour starvation window every
session would have been write-blocked, while the thing actually at risk (certification)
is a small minority of operations.

**Staleness is defined LOCALLY — no network.** A gate must not acquire a dependency on the
remote being reachable, and every signal needed is already on disk:

* ``push-pending`` — :mod:`rebar._store.push_state`'s durable marker is set, meaning a
  terminal delivery failure is outstanding: this clone holds committed ticket events that
  the shared store has never seen. Anything this gate certifies is certified against a view
  other readers do not have.
* ``behind`` — HEAD is a strict ancestor of the already-fetched remote-tracking ref. The
  clone's own git dir proves it is reading an out-of-date view; no fetch is needed to know
  it. ``fsck``'s ``_tracker_sync_status`` deliberately stays silent here because a WRITER
  ff-adopts on its next push — but a gate is a pure READER and never triggers that
  adoption, so for this purpose it is the sharpest signal there is.
* ``diverged`` — neither ref is an ancestor of the other (or there is no common ancestor).
  ``fsck`` already counts this as an integrity issue.

**The probe fails OPEN; the gate fails CLOSED on a PROVEN stale verdict.** Those are not in
tension, they are the only defensible split. A diagnostic that cannot read its own inputs
must not be able to convince a healthy store that it is broken — that is
:func:`rebar._store.push_state.read_status`'s posture and this module keeps it. But once
staleness is established, the gate refuses, because a gate that cannot trust its input has
no business certifying with it.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

#: The verdict a gate reports when it refused because its store was stale. A DISTINCT
#: verdict, never reused from the attestation vocabulary: "we could not trust the input"
#: and "the attestation is stale" are different facts with different remedies, and an
#: operator who cannot tell them apart re-runs the wrong recovery.
STALE_VERDICT = "stale-store"

#: Verdicts that mean the store is not a sound basis for certification, worst first. The
#: order is the reporting precedence when more than one holds.
_STALE_VERDICTS = ("diverged", "behind", "push-pending")

_GIT_TIMEOUT = 60


def _remote_ref() -> str | None:
    """``<remote>/<branch>`` for this tracker, or ``None`` when it cannot be resolved.

    ``tickets.remote`` / ``tickets.branch`` are CODE-repo config (``rebar.toml`` lives in the
    checkout, not beside a relocated store), so resolve the code root the config way — NOT the
    tracker's parent — the same way ``fsck_tracker_health._tracker_sync_status`` does. A
    local-only store, an unconfigured remote, or a never-fetched branch all yield ``None``,
    which reads as "nothing to compare against" rather than as staleness.
    """
    from rebar import config

    try:
        base = config.repo_root_or_none()
        ref = f"{config.tickets_remote(base)}/{config.tickets_branch(base)}"
    except Exception:  # noqa: BLE001 — an unresolvable config is "no basis", not "stale"
        return None
    return ref


def _ref_divergence(tracker: str, remote_ref: str) -> tuple[str, int] | None:
    """``(verdict, behind_count)`` when HEAD is behind/diverged from ``remote_ref``.

    ``None`` means level or strictly ahead — neither is a stale READ: a clone that is
    ahead has every event the remote has, plus its own. (Its own being unpublished is the
    separate ``push-pending`` signal.) Purely local: no ``fetch``, so this reports what the
    clone's git dir ALREADY knows and never makes a gate depend on network reachability.
    """
    from rebar._store.gitutil import run_git

    # raw-git-ok: read-only ancestry probe, mirroring fsck_tracker_health._tracker_sync_status
    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return run_git(tracker, *args, check=False, timeout=_GIT_TIMEOUT)

    if _git("rev-parse", "--verify", remote_ref).returncode != 0:
        return None  # never fetched — no basis for comparison
    if _git("merge-base", "HEAD", remote_ref).returncode != 0:
        return ("diverged", 0)  # unrelated histories
    if _git("merge-base", "--is-ancestor", remote_ref, "HEAD").returncode == 0:
        return None  # remote is an ancestor of HEAD: level or ahead
    if _git("merge-base", "--is-ancestor", "HEAD", remote_ref).returncode != 0:
        return ("diverged", 0)  # common ancestor, neither side an ancestor
    cp = _git("rev-list", f"HEAD..{remote_ref}", "--count")
    raw = (cp.stdout or "").strip()
    return ("behind", int(raw) if raw.isdigit() else 0)


def resolve_tracker(repo_root: Any = None) -> str | None:
    """This repo's tracker path for a gate's freshness probe, or ``None`` to let the probe
    discover it. Best-effort: an unresolvable root must degrade to the default discovery
    rather than turn a freshness check into a gate crash.
    """
    try:
        from rebar import config

        return str(config.tracker_dir(repo_root))
    except Exception:  # noqa: BLE001 — fall back to the probe's own discovery
        return None


def store_freshness(tracker: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Whether this clone's ticket store is a sound basis for certification. Never raises.

    Returns ``{"fresh": bool, "verdict": str, "reason": str, "unpushed": str|None,
    "behind": int|None}`` where ``verdict`` is ``fresh`` or one of :data:`_STALE_VERDICTS`.
    ``tracker`` defaults to the configured store for the current repo.

    Any failure of the probe itself resolves to ``fresh`` (see the module docstring): a
    broken diagnostic must never be able to block a healthy gate.
    """
    result: dict[str, Any] = {
        "fresh": True,
        "verdict": "fresh",
        "reason": "the local ticket store is level with the shared store",
        "unpushed": None,
        "behind": None,
    }
    try:
        if tracker is None:
            from rebar.config import tracker_dir

            tracker = tracker_dir()
        tracker = str(tracker)
        from rebar._store import push_state

        status = push_state.read_status(tracker)
        pending = status.get("state") == "pending"
        remote_ref = _remote_ref()
        divergence = _ref_divergence(tracker, remote_ref) if remote_ref else None
    # A broken probe reports a healthy store, never a stale one.
    except Exception:
        logger.debug("store freshness probe failed; reporting fresh", exc_info=True)
        return result

    if divergence is not None:
        verdict, behind = divergence
        result["behind"] = behind
        if verdict == "diverged":
            result["reason"] = (
                "the local ticket store has DIVERGED from the shared store — neither "
                "history contains the other, so this clone can neither see the shared "
                "state nor publish its own"
            )
        else:
            result["reason"] = (
                f"the local ticket store is {behind} commit(s) BEHIND the shared store — "
                "ticket events written elsewhere are not in this clone's view"
            )
    elif pending:
        verdict = "push-pending"
        result["unpushed"] = str(status.get("unpushed", "unknown"))
        result["reason"] = (
            f"the local ticket store holds {result['unpushed']} committed ticket "
            f"event(s) the shared store has never received "
            f"(last delivery failure: {status.get('reason', 'unknown')})"
        )
    else:
        return result

    result["fresh"] = False
    result["verdict"] = verdict
    return result


def stale_gate_message(gate_label: str, ticket_id: str, freshness: dict[str, Any]) -> str:
    """The refusal text a gate shows when it declined to certify against a stale store.

    Actionable by construction: it NAMES the condition (which of the three, with its
    count), says why refusing is the safe answer, and gives the concrete recovery —
    including the local-CLI fallback, which is the whole point when the surface that went
    stale is a shared MCP server the caller cannot restart.
    """
    return (
        f"Error: {gate_label} refused to certify {ticket_id}: "
        f"{freshness.get('reason', 'the local ticket store is not current')} "
        f"({STALE_VERDICT}: {freshness.get('verdict')}).\n"
        "  Certifying against a store that is not current produces a WRONG verdict — an\n"
        "  attestation can read as unsigned purely because this clone never received it —\n"
        "  so the gate declines rather than record a certification it cannot stand behind.\n"
        "  Recovery:\n"
        "    rebar fsck                     # confirm the condition on THIS store\n"
        "    rebar tracker-maintenance      # reconcile a store that cannot self-heal\n"
        "  If this ran through an MCP server whose clone is stale, re-run the gate against\n"
        "  a local checkout's store instead (the local `rebar` CLI reads its own clone)."
    )


def assert_gate_store_fresh(
    gate_label: str,
    ticket_id: str,
    repo_root: Any = None,
    *,
    tracker: str | os.PathLike[str] | None = None,
) -> None:
    """Refuse a gate operation whose store is not current. Raises ``CommandError``.

    For the gate paths that RAISE rather than return a verdict payload (the
    completion-verification close gate). Paths that already return a
    ``{ok, verdict, reason}`` dict call :func:`store_freshness` directly and report
    :data:`STALE_VERDICT`, so the refusal travels their own channel.
    """
    freshness = store_freshness(tracker if tracker is not None else resolve_tracker(repo_root))
    if freshness["fresh"]:
        return
    from rebar._commands._seam import CommandError

    raise CommandError(stale_gate_message(gate_label, ticket_id, freshness), returncode=1)
