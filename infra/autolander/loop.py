"""Serial auto-lander core loop (epic f1fa / S2 — S2a portion: selection + rebase-routing).

S2a implements the loop's FIRST step: pick the front `Autosubmit`+submittable change/chain
(FIFO by the `Autosubmit` vote's approval date) and route it to the correct Gerrit rebase
call. The wipChain state machine (S2b), fresh-Verified-await + ancestor-atomic submit (S2c),
and failure handling (S3) build on this.

The Gerrit helper (`gerrit.py`) is strictly stdlib-only; this loop MAY `import rebar` for
ticket ops (e.g. annotate the ticket on merge), per the epic's scope note.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from autolander.gerrit import GerritClient

# Selection query: open, has an Autosubmit +1, and Gerrit already considers it submittable
# (both gate votes at MAX AND on-tip under Fast-Forward-Only main, ADR-0040).
SELECTION_QUERY = "status:open label:Autosubmit+1 is:submittable"

# Change classification (routes the rebase call).
KIND_SINGLE = "single"  # a lone non-merge change -> POST /rebase
KIND_CHAIN = "chain"  # a >1-member linear non-merge relation chain -> POST /rebase:chain
KIND_MERGE = (
    "merge"  # a --no-ff merge change -> POST /rebase (first-parent only), NEVER rebase:chain
)


@dataclass
class Candidate:
    """The selected front change/chain to land."""

    change_id: str
    number: int
    autosubmit_date: str  # the Autosubmit +1 ApprovalInfo.date (FIFO key)
    kind: str  # one of KIND_SINGLE / KIND_CHAIN / KIND_MERGE
    member_ids: list[str] = field(
        default_factory=list
    )  # chain members bottom->top; [change_id] otherwise


def autosubmit_approval_date(change: dict) -> str | None:
    """Return the `date` of the `Autosubmit` label's +1 ApprovalInfo on `change`
    (from an `o=DETAILED_LABELS` query), or None when there is no such vote."""
    label = (change.get("labels") or {}).get("Autosubmit") or {}
    for approval in label.get("all") or []:
        if approval.get("value") == 1:
            return approval.get("date")
    return None


def classify_change(client: GerritClient, change: dict) -> tuple[str, list[str]]:
    """Classify `change` for rebase-routing.

    Returns `(kind, member_ids)`:
      - KIND_MERGE  when the current revision commit has >1 parent (a --no-ff merge).
      - KIND_CHAIN  when RelatedChanges reports a >1-member linear (non-merge) relation chain.
      - KIND_SINGLE otherwise.
    `member_ids` are the chain members (bottom-most first) for a chain, else `[change_id]`.
    """
    change_id = change.get("change_id")

    # Merge detection takes precedence: >1 parent on the current revision commit.
    parents = None
    current = change.get("current_revision")
    revisions = change.get("revisions") or {}
    rev = revisions.get(current) if current else None
    if isinstance(rev, dict):
        parents = (rev.get("commit") or {}).get("parents")
    if parents is None:
        fetched = client.get_change(change_id, ["CURRENT_REVISION", "CURRENT_COMMIT"])
        cur = fetched.get("current_revision")
        frev = (fetched.get("revisions") or {}).get(cur) or {}
        parents = (frev.get("commit") or {}).get("parents") or []
    if len(parents) > 1:
        return KIND_MERGE, [change_id]

    # Chain: RelatedChanges with >1 OPEN member, in the order given. A MERGED/ABANDONED
    # related change is NOT part of the live stack — a merged ancestor is now part of `main`
    # (e.g. a just-landed parent), so it must be excluded or the loop mistakes a lone change
    # atop a freshly-merged parent for a 2-member chain and rebases a stack that is already
    # up to date.
    related = [
        m for m in client.get_related(change_id) if m.get("status") not in ("MERGED", "ABANDONED")
    ]
    if len(related) > 1:
        member_ids = [m.get("change_id") or m.get("_change_number") for m in related]
        return KIND_CHAIN, member_ids

    return KIND_SINGLE, [change_id]


def select_front_candidate(client: GerritClient) -> Candidate | None:
    """Select the front change/chain to land: the OLDEST-voted submittable `Autosubmit`
    change (FIFO on the Autosubmit vote's `ApprovalInfo.date`). Returns None when the pool
    is empty."""
    changes = client.query_changes(SELECTION_QUERY, ["DETAILED_LABELS"])
    if not changes:
        return None

    dated = [(autosubmit_approval_date(c), c) for c in changes]
    dated = [(d, c) for (d, c) in dated if d is not None]
    if not dated:
        return None

    date, change = min(dated, key=lambda pair: pair[0])
    kind, member_ids = classify_change(client, change)
    return Candidate(
        change_id=change.get("change_id"),
        number=change.get("_number"),
        autosubmit_date=date,
        kind=kind,
        member_ids=member_ids,
    )


def route_rebase(client: GerritClient, candidate: Candidate) -> str:
    """Route `candidate` to the correct Gerrit rebase call, preserving the uploader
    (`rebase_on_behalf_of_uploader=true`, so author/DCO/`rebar-ticket` trailers survive and
    the rebase drops `Verified` -> CI re-runs). Returns the endpoint kind actually invoked:
    `"rebase:chain"` for a KIND_CHAIN candidate, `"rebase"` for KIND_SINGLE / KIND_MERGE."""
    if candidate.kind == KIND_CHAIN:
        client.rebase_chain(candidate.change_id, on_behalf_of_uploader=True)
        return "rebase:chain"
    client.rebase(candidate.change_id, on_behalf_of_uploader=True)
    return "rebase"


# =====================================================================================
# S2b: the wipChain state machine + Fast-Forward-Only TOCTOU guard + stack hand-back.
# =====================================================================================

# wipChain phases. `paused` is S5's emergency-stop; the rest are the drive lifecycle.
PHASE_SELECTING = "selecting"
PHASE_REBASING = "rebasing"
PHASE_AWAITING_VERIFIED = "awaiting_verified"
PHASE_SUBMITTING = "submitting"
PHASE_IDLE = "idle"
PHASE_PAUSED = "paused"
VALID_PHASES = frozenset(
    {
        PHASE_SELECTING,
        PHASE_REBASING,
        PHASE_AWAITING_VERIFIED,
        PHASE_SUBMITTING,
        PHASE_IDLE,
        PHASE_PAUSED,
    }
)

# Bounded re-drive: under FFO, main can advance faster than we rebase; retry a bounded number
# of times before handing the stack back to its owning agent.
MAX_RE_DRIVE = 5

# The typed reason recorded/announced when a stack is handed back for a rebase.
HANDBACK_NEEDS_REBASE = "needs_rebase"
HANDBACK_COMMENT = (
    "main advanced faster than the lander could rebase; rebase onto current main "
    "and re-apply Autosubmit"
)


@dataclass
class WipChain:
    """The SINGLE in-flight work item (one instance -> never two rebases onto one HEAD)."""

    change_id: str  # the chain tip (or the lone change)
    chain_member_ids: list[str]  # all members bottom->top; [change_id] for a single
    tested_shas: dict = field(default_factory=dict)  # change_id -> the CI-tested revision sha
    verified_at: dict = field(default_factory=dict)  # change_id -> fresh-Verified timestamp
    phase: str = PHASE_IDLE
    re_drive_count: int = 0
    rechecking: bool = False  # S3-owned in-flight sub-state: a bounded auto-`recheck` is pending
    # (guards against re-posting `recheck`); NOT a new S2 phase.


def is_landable(client: GerritClient, wip: WipChain) -> bool:
    """FFO TOCTOU re-check performed immediately before submit: every member is STILL
    `submittable` (under FFO that already implies descendant-of-current-tip, ADR-0040) AND
    its current revision sha is UNCHANGED from the tested sha we recorded. Any drift
    (main advanced, a new patchset, a dropped vote) -> not landable."""
    for member_id in wip.chain_member_ids:
        # SUBMITTABLE is REQUIRED — Gerrit only returns the `submittable` field when it is
        # requested; without it the field is absent (reads as None) and this guard would
        # wrongly report not-landable and rebase an already-submittable on-tip change (the
        # live E2E surfaced exactly this).
        change = client.get_change(member_id, ["CURRENT_REVISION", "SUBMITTABLE"])
        if change.get("submittable") is not True:
            return False
        if change.get("current_revision") != wip.tested_shas.get(member_id):
            return False
    return True


def hand_back(
    client: GerritClient,
    wip: WipChain,
    reason: str = HANDBACK_NEEDS_REBASE,
    *,
    record_handback=None,
) -> None:
    """Hand the whole stack back to its owning agent (never partial-land, never evict a
    member): remove `Autosubmit` (vote 0) from EVERY member and post a Gerrit comment naming
    `reason`; the label-removal is the "handed back" signal `land` reads. `record_handback`,
    when provided, is S3's marker writer `(reason, wip) -> None` (decoupled; S3 owns the
    marker mechanism). Same hand-back mechanism S3 uses for conflict/CI-fail."""
    comment = HANDBACK_COMMENT if reason == HANDBACK_NEEDS_REBASE else f"handed back: {reason}"
    for member_id in wip.chain_member_ids:
        client.set_review(member_id, message=comment, labels={"Autosubmit": 0})
    if record_handback is not None:
        record_handback(reason, wip)
    wip.phase = PHASE_IDLE


def drive_to_submit(
    client: GerritClient,
    wip: WipChain,
    *,
    rebase,
    await_verified,
    record_handback=None,
) -> str:
    """Drive `wip` to a terminal outcome under the FFO TOCTOU guard + bounded re-drive.

    Loop up to MAX_RE_DRIVE times: if `is_landable`, `phase`->submitting and submit (ancestor-
    atomic; S2c refines the submit call) -> return "submitted". Otherwise (or on a
    `not fast-forward` submit refusal) re-drive: `phase`->rebasing, `rebase(client, wip)`,
    `phase`->awaiting_verified, `await_verified(client, wip)`, increment `re_drive_count`, and
    re-check. On exhausting MAX_RE_DRIVE without landing, `hand_back(...)` -> return
    "handed_back". `rebase` and `await_verified` are injected (S2a routing + S2c await); they
    are decoupled so this state machine is unit-testable in isolation."""
    from autolander.gerrit import GerritError

    for _attempt in range(MAX_RE_DRIVE + 1):
        if is_landable(client, wip):
            wip.phase = PHASE_SUBMITTING
            try:
                client.submit(wip.change_id)
            except GerritError as exc:
                if "not fast-forward" not in str(exc).lower():
                    raise
                # not-ff refusal: fall through to a re-drive, treated as not-landable.
            else:
                return "submitted"

        if wip.re_drive_count >= MAX_RE_DRIVE:
            hand_back(client, wip, HANDBACK_NEEDS_REBASE, record_handback=record_handback)
            return "handed_back"

        wip.phase = PHASE_REBASING
        rebase(client, wip)
        wip.phase = PHASE_AWAITING_VERIFIED
        await_verified(client, wip)
        wip.re_drive_count += 1

    hand_back(client, wip, HANDBACK_NEEDS_REBASE, record_handback=record_handback)
    return "handed_back"


# =====================================================================================
# S2c: fresh-Verified-per-member await + ancestor-atomic submit (the landing action).
# =====================================================================================

# Bounded await for a fresh Verified on the rebased tree (matches `land`'s default timeout).
MEMBER_VERIFIED_TIMEOUT_S = 30 * 60
VERIFIED_POLL_INTERVAL_S = 15


# Structured, greppable marker for high-visibility failures. S5c's observability keys its
# `autolander_errors` alarm off this token on the container's stderr; keeping the emission
# here (not only in a caller) makes the partial-land failure loud at its source.
AUTOLANDER_ERROR = "AUTOLANDER_ERROR"
HANDBACK_PARTIAL_LAND = "partial_land"


class PartialLandError(RuntimeError):
    """R3/R5 violation: after an ancestor-atomic submit, some stack member did NOT reach
    MERGED. `ancestor_atomic_submit` emits AUTOLANDER_ERROR + a metric and hands back BEFORE
    raising this, so the failure is loud at its source."""


def emit_autolander_error(
    detail: str, *, emit_metric=None, metric: str = "autolander_partial_land"
) -> None:
    """Emit a high-visibility failure: a structured `AUTOLANDER_ERROR` line on stderr (S5c
    greps this) and, when wired, a metric via the injected `emit_metric(name, value)` hook
    (S5c connects it to CloudWatch; None in tests / unwired runs)."""
    sys.stderr.write(f"{AUTOLANDER_ERROR} {detail}\n")
    sys.stderr.flush()
    if emit_metric is not None:
        emit_metric(metric, 1)


def close_ticket_via_rebar(
    change_id: str, *, ticket_id: str | None = None, message: str | None = None
) -> None:
    """Concrete `import rebar` ticket-annotate seam used on merge (the production default for
    `ancestor_atomic_submit`'s `close_ticket`). Annotates the associated rebar ticket that the
    change landed; best-effort (a ticket-op failure must not crash the lander)."""
    import rebar  # loop.py MAY import rebar for ticket ops (epic scope); gerrit.py may not.

    tid = ticket_id or change_id
    msg = message or f"Landed on main via the serial auto-lander (Gerrit change {change_id})."
    try:
        rebar.comment(tid, msg)
    except Exception as exc:  # noqa: BLE001 — annotation is best-effort; never crash the loop
        sys.stderr.write(f"{AUTOLANDER_ERROR} ticket annotate failed for {tid}: {exc}\n")


def has_fresh_verified(change: dict) -> bool:
    """True iff `change` carries a fresh `Verified +1` on its CURRENT patchset. Because the
    rebase drops `Verified` (copyCondition NO_CODE_CHANGE only), a present `Verified +1` is by
    construction on the post-rebase SHA — not a copied/carried vote."""
    verified = (change.get("labels") or {}).get("Verified") or {}
    if "approved" in verified:
        return True
    for entry in verified.get("all") or []:
        if (entry.get("value") or 0) >= 1:
            return True
    return False


def all_members_fresh_verified(client: GerritClient, wip: WipChain) -> bool:
    """True iff EVERY chain member currently carries a fresh `Verified +1`. Submitting while
    any member lacks one would land an untested tree (violates R5), so this gates submit."""
    for member_id in wip.chain_member_ids:
        change = client.get_change(member_id, ["DETAILED_LABELS", "CURRENT_REVISION"])
        if not has_fresh_verified(change):
            return False
    return True


def await_fresh_verified(
    client: GerritClient,
    wip: WipChain,
    *,
    timeout_s: int = MEMBER_VERIFIED_TIMEOUT_S,
    poll_interval_s: int = VERIFIED_POLL_INTERVAL_S,
    time_fn=None,
    sleep_fn=None,
) -> bool:
    """Poll until EVERY member carries a fresh `Verified +1` (recording the tested SHA +
    verified timestamp on `wip`). Returns True when all are fresh-verified; returns False on
    exceeding `timeout_s` (CI hung) so the caller hands the stack back rather than blocking
    forever. `time_fn`/`sleep_fn` are injectable for tests (default: monotonic clock +
    time.sleep)."""
    time_fn = time_fn or time.monotonic
    sleep_fn = sleep_fn or time.sleep
    start = time_fn()
    while True:
        if all_members_fresh_verified(client, wip):
            for member_id in wip.chain_member_ids:
                change = client.get_change(member_id, ["DETAILED_LABELS", "CURRENT_REVISION"])
                wip.tested_shas[member_id] = change.get("current_revision")
                verified = (change.get("labels") or {}).get("Verified") or {}
                approved = verified.get("approved") or {}
                wip.verified_at[member_id] = approved.get("date") or time_fn()
            return True
        if time_fn() - start >= timeout_s:
            return False
        sleep_fn(poll_interval_s)


_USE_REBAR_CLOSE = object()  # sentinel: default to the concrete import-rebar annotate seam


def ancestor_atomic_submit(
    client: GerritClient,
    wip: WipChain,
    *,
    close_ticket=_USE_REBAR_CLOSE,
    emit_metric=None,
    record_handback=None,
) -> str:
    """Land the stack with ONE `POST /changes/{tip}/submit` — Gerrit submits the members
    "Submitted Together" by relation-chain ancestry (all-or-nothing), reinforced by S3's
    shared topic + `change.submitWholeTopic`. **Partial-land safety (mandatory):** after the
    submit call, verify EVERY member reached `MERGED`; if any is still open, fail LOUDLY —
    emit `AUTOLANDER_ERROR` + a metric, hand the stack back (`record_handback`), and raise
    `PartialLandError` (never proceed on a partial land). On full merge, annotate each
    member's ticket (`close_ticket`; defaults to the concrete `close_ticket_via_rebar`
    `import rebar` seam — pass an explicit callable in tests) and return "merged"."""
    client.submit(wip.change_id)
    not_merged = [m for m in wip.chain_member_ids if client.get_change(m).get("status") != "MERGED"]
    if not_merged:
        detail = "partial land: member(s) did not reach MERGED: " + ", ".join(
            str(m) for m in not_merged
        )
        emit_autolander_error(detail, emit_metric=emit_metric)
        if record_handback is not None:
            record_handback(HANDBACK_PARTIAL_LAND, wip)
        raise PartialLandError(detail)
    closer = close_ticket_via_rebar if close_ticket is _USE_REBAR_CLOSE else close_ticket
    if closer is not None:
        for member_id in wip.chain_member_ids:
            closer(member_id)
    return "merged"


# =====================================================================================
# S5c/S5d: the runnable bot — heartbeat, emergency-stop, status endpoint, markers,
# SIGTERM drain + crash-safe recovery. The single-instance loop that wires the pieces.
# =====================================================================================

POLL_S = 15  # the bot's poll cadence; heartbeat is written once per tick (freshness <= 15s).
HEARTBEAT_FILE = "heartbeat"
EMERGENCY_STOP_FILE = "emergency-stop"
RECOVERY_FILE = "recovery.json"
LOCK_FILE = "autolander.lock"

# S5c markers — a single stdout line: the bare token + one space + a compact JSON object
# (the shared emitter/parser contract observability.sh greps).
MARKER_ERROR = "AUTOLANDER_ERROR"
MARKER_HANDBACK = "AUTOLANDER_HANDBACK"


def emit_marker(token: str, payload: dict) -> None:
    """Write a `<TOKEN> {json}` line to stdout (the observability feeder contract)."""
    sys.stdout.write(token + " " + json.dumps(payload, sort_keys=True) + "\n")


def write_heartbeat(state_dir, now: float) -> None:
    """Write `now` (epoch seconds) to the heartbeat file in the state volume (once per tick)."""
    (Path(state_dir) / HEARTBEAT_FILE).write_text(str(now))


def heartbeat_age_s(state_dir, now: float) -> int:
    """Integer seconds since the last heartbeat write (a huge value when absent) — the exact
    `heartbeat_age_s` the status endpoint serves and `land`'s `lander_down` binds to."""
    try:
        contents = (Path(state_dir) / HEARTBEAT_FILE).read_text()
        return int(now - float(contents))
    except (OSError, ValueError):
        return 10**9


def is_emergency_stopped(state_dir) -> bool:
    """True iff the emergency-stop sentinel file exists (operator creates to pause). Lives on
    the state volume so a redeploy does not silently un-pause."""
    return (Path(state_dir) / EMERGENCY_STOP_FILE).exists()


def build_status(wip, *, heartbeat_age_s: int, waiting_count: int, time_in_phase_s: int) -> dict:
    """The read-only status JSON: the current wipChain (change, phase, tested_shas,
    time_in_phase_s), the count of Autosubmit-set-and-waiting changes, and heartbeat_age_s."""
    return {
        "heartbeat_age_s": heartbeat_age_s,
        "waiting_count": waiting_count,
        "time_in_phase_s": time_in_phase_s,
        "phase": wip.phase,
        "change": wip.change_id,
        "chain_member_ids": list(wip.chain_member_ids),
        "tested_shas": dict(wip.tested_shas),
    }


def write_recovery(state_dir, wip, *, acknowledged: bool = True) -> None:
    """SIGTERM's FIRST action: snapshot the in-flight wipChain to `recovery.json` on the state
    volume — `change_id`, `chain_member_ids`, `tested_shas`, `phase`, `re_drive_count`, and the
    S3 hand-back `acknowledged` flag — so a crash/restart mid-flight can reconcile per-phase
    rather than double-submit or strand a stack. `acknowledged` defaults True (a normal drive
    snapshot has no pending hand-back); pass `acknowledged=False` ONLY when snapshotting while
    a hand-back's Autosubmit-removal is still in progress, so restart re-drives it to
    completion."""
    snapshot = {
        "change_id": wip.change_id,
        "chain_member_ids": list(wip.chain_member_ids),
        "tested_shas": dict(wip.tested_shas),
        "phase": wip.phase,
        "re_drive_count": wip.re_drive_count,
        "acknowledged": acknowledged,
    }
    (Path(state_dir) / RECOVERY_FILE).write_text(json.dumps(snapshot, sort_keys=True))


def load_recovery(state_dir):
    """Load the `recovery.json` snapshot into a `(WipChain, acknowledged)` pair, or None when
    absent. (Older records without `acknowledged` default to True — nothing to re-drive.)"""
    path = Path(state_dir) / RECOVERY_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    wip = WipChain(
        change_id=data["change_id"],
        chain_member_ids=data["chain_member_ids"],
        tested_shas=data["tested_shas"],
        phase=data.get("phase", PHASE_IDLE),
        re_drive_count=data.get("re_drive_count", 0),
    )
    return wip, data.get("acknowledged", True)


def reconcile_recovery(client: GerritClient, state_dir) -> str | None:
    """On restart, reconcile a `recovery.json` snapshot against LIVE Gerrit BEFORE resuming,
    with a rule per wipChain phase (idempotent; NEVER double-submits; NEVER strands a stack).
    Returns a short disposition (or None when there was no recovery record).

    Precedence: (1) tip already MERGED → clear (the land completed). (2) An UNACKNOWLEDGED
    hand-back (`acknowledged=False`) → re-drive `Autosubmit` removal to completion, then clear.
    (3) tested SHA drifted (main moved / new patchset) → discard + re-select. (4) Per phase for
    a still-open, SHA-matching change: `rebasing`/`awaiting_verified`/`selecting` → discard +
    re-select (the step never completed); `submitting` (and not MERGED) → discard + re-select
    (re-check submittability, never blind re-submit); `idle`/`paused` → nothing in flight,
    clear."""
    loaded = load_recovery(state_dir)
    if loaded is None:
        return None
    wip, acknowledged = loaded
    recovery_path = Path(state_dir) / RECOVERY_FILE
    tip = client.get_change(wip.change_id, ["CURRENT_REVISION", "SUBMITTABLE"])

    if tip.get("status") == "MERGED":
        recovery_path.unlink(missing_ok=True)
        return f"phase={wip.phase}: already merged; recovery cleared"

    if not acknowledged:
        # a hand-back's Autosubmit removal was interrupted -> complete it idempotently.
        from autolander import failure

        failure.remove_autosubmit_from_stack(client, wip)
        recovery_path.unlink(missing_ok=True)
        return f"phase={wip.phase}: unacknowledged hand-back; Autosubmit removal re-driven; cleared"

    if tip.get("current_revision") != wip.tested_shas.get(wip.change_id):
        recovery_path.unlink(missing_ok=True)
        return f"phase={wip.phase}: stale SHA (main advanced); discard + re-select"

    # still-open, SHA-matching change: every phase discards + re-selects (the loop re-drives
    # from a clean slate; submitting is NOT blind-resubmitted since it's not MERGED here).
    recovery_path.unlink(missing_ok=True)
    if wip.phase in (PHASE_IDLE, PHASE_PAUSED):
        return f"phase={wip.phase}: nothing in flight; cleared"
    return f"phase={wip.phase}: interrupted before completion; re-select + re-drive"


def make_status_server(state, gerrit, root, *, port=8080):
    """Build the read-only status HTTP server (extracted from run_loop so the endpoint is
    unit-testable via a real GET). The `_Status` handler closes over `state` (the
    `{"wip", "phase_since"}` dict), `gerrit` (the waiting-count query), and `root` (the state
    dir for `heartbeat_age_s`). Returns a bound `HTTPServer`; the caller starts/serves it on a
    daemon thread and `shutdown()`s it. Behaviour is byte-for-byte the run_loop original."""
    import http.server

    class _Status(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            wip = state["wip"] or WipChain(change_id="", chain_member_ids=[], phase=PHASE_IDLE)
            body = json.dumps(
                build_status(
                    wip,
                    heartbeat_age_s=heartbeat_age_s(root, time.time()),
                    waiting_count=len(gerrit.query_changes(SELECTION_QUERY, ["DETAILED_LABELS"])),
                    time_in_phase_s=int(time.monotonic() - state["phase_since"]),
                )
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):  # silence default stderr access log
            return

    return http.server.HTTPServer(("0.0.0.0", port), _Status)  # noqa: S104 — loopback via nginx


def healthcheck_ok(state_dir, now=None, *, stale_s=90) -> bool:
    """The container HEALTHCHECK predicate, SHARED with the S5d test: the heartbeat is fresh
    (healthy) iff its age is within `stale_s`; age > `stale_s` => unhealthy. `now` defaults to
    wall-clock time. Container and test exercise this exact same age>90 => unhealthy logic."""
    return heartbeat_age_s(state_dir, now if now is not None else time.time()) <= stale_s


def healthcheck_main() -> None:  # pragma: no cover
    """Docker HEALTHCHECK entrypoint (`python -c "from autolander.loop import healthcheck_main;
    healthcheck_main()"`): exit 0 (healthy) iff the container's heartbeat is fresh, else 1
    (unhealthy -> the autoheal sidecar restarts the wedged loop). The container state dir is
    fixed at /var/gerrit/site/autolander (matches the Dockerfile + run defaults)."""
    import sys

    sys.exit(0 if healthcheck_ok("/var/gerrit/site/autolander") else 1)


def handle_sigterm(state, stopping, root, *, now, grace_s=120) -> None:
    """SIGTERM handler body (extracted so the drain contract is unit-testable). FIRST snapshot
    the in-flight wipChain to recovery.json (crash-safe), THEN flip to draining: stop taking
    new work (`stopping["v"]=True`) and set the bounded drain deadline (`now + grace_s`). Order
    matters — the recovery snapshot must be durable before we begin draining."""
    if state["wip"] is not None:
        write_recovery(root, state["wip"])
    stopping["v"] = True
    stopping["drain_deadline"] = now + grace_s


def run_loop(*, state_dir, gerrit, marker_store, status_port=8080):  # pragma: no cover
    """The single-instance bot entrypoint (operational glue over the tested pieces; exercised
    by the live E2E, not unit tests). Acquire the flock (single instance), reconcile any
    recovery.json, start the read-only status HTTP server on `status_port`, then poll every
    POLL_S: write the heartbeat; if the emergency-stop sentinel is present stay `paused`; else
    select the front candidate, drive it to submit, handling failure/hand-back. On SIGTERM,
    FIRST write recovery.json, then drain the in-flight wipChain (bounded) before exit."""
    import fcntl
    import signal
    import threading

    from autolander.gerrit import GerritError  # noqa: F401 — used in the drive/except paths

    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock_fd = open(root / LOCK_FILE, "w")  # noqa: SIM115 — held for process lifetime
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        emit_marker(MARKER_ERROR, {"detail": "another instance holds the lock"})
        return 1

    reconcile_recovery(gerrit, root)

    state = {"wip": None, "phase_since": time.monotonic()}

    httpd = make_status_server(state, gerrit, root, port=status_port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    stopping = {"v": False, "drain_deadline": None}

    # A daemon heartbeat thread writes the heartbeat every POLL_S INDEPENDENTLY of the main
    # loop — so it stays fresh (< 15 s) during a long in-flight drive AND during the SIGTERM
    # drain, keeping the container `healthy` so autoheal never restarts a still-working bot.
    def _heartbeat_loop():
        while True:
            write_heartbeat(root, time.time())
            time.sleep(POLL_S)

    write_heartbeat(root, time.time())  # one synchronous write so the file exists immediately
    threading.Thread(target=_heartbeat_loop, daemon=True).start()

    def _on_sigterm(_signum, _frame):
        # FIRST action: crash-safe snapshot; then DRAIN — stop taking new work but let the
        # in-flight wipChain finish, bounded by the 120 s stop_grace_period (compose), while
        # the heartbeat thread keeps the container healthy. (Body extracted to handle_sigterm
        # so the drain contract is unit-testable.)
        handle_sigterm(state, stopping, root, now=time.monotonic())

    signal.signal(signal.SIGTERM, _on_sigterm)

    while not stopping["v"]:
        if is_emergency_stopped(root):
            time.sleep(POLL_S)
            continue
        try:
            cand = select_front_candidate(gerrit)
            if cand is not None:
                wip = WipChain(change_id=cand.change_id, chain_member_ids=cand.member_ids)
                state["wip"], state["phase_since"] = wip, time.monotonic()
                _drive_candidate(gerrit, wip, cand, marker_store, root)
                state["wip"] = None
        except Exception as exc:  # noqa: BLE001 — never wedge silently; hand back + stay loud
            emit_marker(
                MARKER_ERROR,
                {"detail": str(exc), "change": (state["wip"].change_id if state["wip"] else "")},
            )
            state["wip"] = None
        time.sleep(POLL_S)
    return 0


def _drive_candidate(gerrit, wip, cand, marker_store, root):
    """Drive one selected candidate to a terminal outcome, composing the tested S2/S3 pieces.

    SUBMIT-FIRST, rebase-on-conflict (do NOT rebase upfront): a selected change is already
    `is:submittable`, which under FFO means it is ALREADY on the current tip with fresh votes
    — so its tested tree IS the merged tree and it submits directly. A rebase is needed ONLY
    when main advances between selection and submit (the submit then refuses `not
    fast-forward`); `drive_to_submit` handles that by rebasing to the new tip, awaiting a fresh
    Verified, and retrying (bounded), then handing back on exhaustion. On a post-rebase
    `Verified -1`, run the bounded auto-recheck and hand back on repeat."""
    from autolander import failure  # local import: loop.py stays import-cycle-free

    # The selected change is submittable now (on-tip, fresh votes): record its tested SHAs so
    # is_landable's TOCTOU guard passes for a direct submit.
    for mid in wip.chain_member_ids:
        wip.tested_shas[mid] = gerrit.get_change(mid, ["CURRENT_REVISION"]).get("current_revision")

    from autolander.gerrit import GerritError

    for attempt in range(MAX_RE_DRIVE + 1):
        if is_landable(gerrit, wip):
            wip.phase = PHASE_SUBMITTING
            try:
                ancestor_atomic_submit(gerrit, wip)  # submit + partial-land guard + ticket close
                return
            except GerritError as exc:
                if "not fast-forward" not in str(exc).lower():
                    raise  # a real submit error, not the TOCTOU race
        # not landable (main advanced) or a not-ff submit refusal -> re-drive, bounded
        if attempt >= MAX_RE_DRIVE:
            failure.handle_rebase_conflict(gerrit, wip, marker_store, stack_id=cand.change_id)
            return
        wip.phase = PHASE_REBASING
        try:
            route_rebase(gerrit, cand)  # rebase onto the new tip (on behalf of the uploader)
        except GerritError:
            # POST /rebase 409 = a textual rebase CONFLICT (never allow_conflicts): record
            # needs_rebase + remove Autosubmit from the whole stack, hand back to the owner.
            failure.handle_rebase_conflict(gerrit, wip, marker_store, stack_id=cand.change_id)
            return
        wip.phase = PHASE_AWAITING_VERIFIED
        wip.re_drive_count = attempt + 1
        if not await_fresh_verified(gerrit, wip):  # refreshes tested_shas on success
            if (
                failure.auto_recheck(gerrit, wip, await_terminal_verified=_terminal_verified_vote)
                != failure.OUTCOME_VERIFIED
            ):
                failure.handle_ci_fail(gerrit, wip, marker_store)  # post-rebase -1 survived recheck
                return


def _terminal_verified_vote(
    gerrit, wip, *, time_fn=None, sleep_fn=None, timeout_s=None, poll_s=None
):
    """Await the tip's TERMINAL Verified vote for the auto-recheck. The `gerrit-verify`
    clear-vote job resets `Verified -> 0` at run start; this MUST be treated as
    *recheck-running*, NOT as a result — so a transient `0`/absent vote keeps polling. Returns
    `+1` on a terminal approved, `-1` on a terminal rejected, and `-1` on timeout (give up ->
    hand back). `time_fn`/`sleep_fn` are injectable for tests."""
    time_fn = time_fn or time.monotonic
    sleep_fn = sleep_fn or time.sleep
    timeout_s = MEMBER_VERIFIED_TIMEOUT_S if timeout_s is None else timeout_s
    poll_s = VERIFIED_POLL_INTERVAL_S if poll_s is None else poll_s
    start = time_fn()
    while True:
        verified = (gerrit.get_change(wip.change_id, ["DETAILED_LABELS"]).get("labels") or {}).get(
            "Verified"
        ) or {}
        if verified.get("approved"):
            return 1
        if verified.get("rejected"):
            return -1
        # transient 0 / no terminal result yet -> the recheck is still running; keep waiting.
        if time_fn() - start >= timeout_s:
            return -1
        sleep_fn(poll_s)


def main(argv=None):  # pragma: no cover
    """Container entrypoint: `python -m autolander.loop`. Wires the real Gerrit client + state
    volume from the environment and runs the loop."""
    import os

    from autolander.failure import MarkerStore
    from autolander.gerrit import GerritClient

    base = os.environ.get("REBAR_GERRIT_URL", "https://rebar.solutions.navateam.com/a")
    user = os.environ.get("AUTOLANDER_GERRIT_USER", "RebarBotNava")
    token = os.environ.get("AUTOLANDER_GERRIT_TOKEN", "")
    state_dir = os.environ.get("REBAR_AUTOLANDER_STATE_DIR", "/var/gerrit/site/autolander")
    gerrit = GerritClient(base, user, token)
    return run_loop(state_dir=state_dir, gerrit=gerrit, marker_store=MarkerStore(state_dir))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
