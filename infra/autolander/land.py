"""Agent landing contract (epic f1fa / S4): the `land` / `land-status` command.

ONE call → ONE typed terminal outcome + a distinct exit code, so agents never correlate
votes/labels/status themselves (the "both votes green but conflicts" trap is handled here).
Project tooling — NOT `rebar` core; may `import rebar` only for ticket ops. Gerrit access
reuses the stdlib `GerritClient` (urllib+Basic-auth+XSSI); the contract is documented at
`docs/land-contract.md`.

Testability: the Gerrit client, the S5 status-endpoint reader, the S3 marker lookup, the
Autosubmit setter, and the clock/sleep are all injected, so the outcome logic is unit-tested
with fakes (a live E2E check covers the real path).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

# --- the versioned outcome enum → exit-code contract (docs/land-contract.md) --------------
CONTRACT_VERSION = "1"
MERGED = "merged"
NEEDS_REBASE = "needs_rebase"
CI_FAILED = "ci_failed"
REVIEW_FAILED = "review_failed"
NOT_REQUESTED = "not_requested"
ABANDONED = "abandoned"
LANDER_DOWN = "lander_down"
ERROR = "error"
PENDING = "pending"
TIMED_OUT = "timed_out"

EXIT_CODES = {
    MERGED: 0,
    NEEDS_REBASE: 1,
    CI_FAILED: 2,
    REVIEW_FAILED: 3,
    NOT_REQUESTED: 4,
    ABANDONED: 5,
    LANDER_DOWN: 6,
    ERROR: 7,
    PENDING: 75,
    TIMED_OUT: 124,
}

LANDER_DOWN_MESSAGE = "lander unavailable — rebase to tip and submit manually (see CONTRIBUTING §2e 'Submit', ADR-0040)."  # noqa: E501

# Operational thresholds — config values coupled to S5's 15 s bot poll (read from env/config,
# NOT hard-coded literals, so an S5 poll-rate change is matched without a code edit).
HEARTBEAT_STALE_S = int(os.environ.get("REBAR_AUTOLANDER_HEARTBEAT_STALE_S", "90"))
POLL_INTERVAL_S = int(os.environ.get("REBAR_AUTOLANDER_POLL_S", "30"))
WAIT_TIMEOUT_S = int(os.environ.get("REBAR_AUTOLANDER_WAIT_TIMEOUT_S", str(30 * 60)))


def exit_code_for(outcome: str) -> int:
    """Map an outcome to its pinned exit code (ERROR's code for an unknown outcome)."""
    return EXIT_CODES.get(outcome, EXIT_CODES[ERROR])


def heartbeat_fresh(status_reader, *, stale_s: int = HEARTBEAT_STALE_S) -> bool:
    """Read the bot's `heartbeat_age_s` from S5's status endpoint via `status_reader()` (a
    callable returning the status dict, or raising on an unreachable endpoint). Fresh iff
    `heartbeat_age_s < stale_s`. An unreachable endpoint (any exception) is treated
    CONSERVATIVELY as NOT fresh (liveness cannot be confirmed → lander_down), never as error."""
    try:
        status = status_reader()
        return status["heartbeat_age_s"] < stale_s
    except Exception:  # noqa: BLE001 — any failure is conservatively "not fresh"
        return False


def derive_outcome(native_change: dict | None, needs_rebase_marker) -> str | None:
    """Fallback precedence: (1) if the bot recorded `needs_rebase` (a valid S3 marker) → return
    NEEDS_REBASE; else derive from native Gerrit state — status MERGED → MERGED, ABANDONED →
    ABANDONED, `Verified -1` → CI_FAILED, `LLM-Review -1` → REVIEW_FAILED (if BOTH -1 and no
    marker, CI_FAILED wins). Returns None when not yet terminal. `needs_rebase` is NEVER
    derivable from native state (under FFO a behind-tip change is merely non-submittable)."""
    if needs_rebase_marker:
        return NEEDS_REBASE
    if not native_change:
        return None
    status = native_change.get("status")
    if status == "MERGED":
        return MERGED
    if status == "ABANDONED":
        return ABANDONED
    labels = native_change.get("labels") or {}
    if _label_rejected(labels.get("Verified")):
        return CI_FAILED
    if _label_rejected(labels.get("LLM-Review")):
        return REVIEW_FAILED
    return None


def _label_rejected(label: dict | None) -> bool:
    """A label is `-1` when it has a `rejected` entry or any `all` vote with value <= -1."""
    if not isinstance(label, dict):
        return False
    if label.get("rejected"):
        return True
    for entry in label.get("all") or []:
        if isinstance(entry, dict) and entry.get("value", 0) <= -1:
            return True
    return False


def _label_approved(label: dict | None, *, threshold: int = 1) -> bool:
    """A label has a positive vote when any `all` entry's value >= `threshold`."""
    if not isinstance(label, dict):
        return False
    for entry in label.get("all") or []:
        if isinstance(entry, dict) and entry.get("value", 0) >= threshold:
            return True
    return False


def land(
    change: str,
    *,
    gerrit,
    status_reader,
    marker_lookup,
    set_autosubmit,
    clock,
    sleep,
    poll_s: int = POLL_INTERVAL_S,
    timeout_s: int = WAIT_TIMEOUT_S,
    stale_s: int = HEARTBEAT_STALE_S,
) -> tuple[str, dict]:
    """Request a land and block until terminal. Heartbeat-FIRST: if the bot heartbeat is stale
    / unreachable at invocation, return `(LANDER_DOWN, …)` WITHOUT setting `Autosubmit` (never
    orphan a label the down bot won't consume). When fresh, `set_autosubmit(change)` (the
    agent's OWN identity), then poll every `poll_s`: consult native Gerrit
    (`gerrit.get_change(change, ["DETAILED_LABELS"])`) + the S3 marker (`marker_lookup(change)`)
    via `derive_outcome`; return on a terminal outcome; re-check heartbeat each cycle (stale →
    LANDER_DOWN); on `timeout_s` → TIMED_OUT. Returns `(outcome, detail_dict)`."""
    if not heartbeat_fresh(status_reader, stale_s=stale_s):
        return (LANDER_DOWN, {"detail": LANDER_DOWN_MESSAGE})
    set_autosubmit(change)
    start = clock()
    errors = 0
    while True:
        if not heartbeat_fresh(status_reader, stale_s=stale_s):
            return (LANDER_DOWN, {"detail": LANDER_DOWN_MESSAGE})
        try:
            native = gerrit.get_change(change, ["DETAILED_LABELS"])
        except Exception as exc:  # noqa: BLE001 — bounded transport-error tolerance
            errors += 1
            if errors >= 3:
                return (ERROR, {"detail": str(exc)})
            sleep(poll_s)
            continue
        marker = marker_lookup(change)
        outcome = derive_outcome(native, marker)
        if outcome is not None:
            return (outcome, {})
        if clock() - start >= timeout_s:
            return (TIMED_OUT, {})
        sleep(poll_s)


def land_status(
    change: str,
    *,
    gerrit,
    status_reader,
    marker_lookup,
    stale_s: int = HEARTBEAT_STALE_S,
) -> tuple[str, dict]:
    """Single-shot status (never sets `Autosubmit`). Same fallback precedence as `land`, PLUS:
    `PENDING` while the bot is still driving (a bot record or `Autosubmit` present but not yet
    terminal), and `NOT_REQUESTED` on a change with NO bot record AND no `Autosubmit` that is
    not already natively terminal. Heartbeat stale/unreachable → `LANDER_DOWN`."""
    if not heartbeat_fresh(status_reader, stale_s=stale_s):
        return (LANDER_DOWN, {"detail": LANDER_DOWN_MESSAGE})
    native = gerrit.get_change(change, ["DETAILED_LABELS"])
    marker = marker_lookup(change)
    outcome = derive_outcome(native, marker)
    if outcome is not None:
        return (outcome, {})
    labels = (native or {}).get("labels") or {}
    if _label_approved(labels.get("Autosubmit")) or marker:
        return (PENDING, {})
    return (NOT_REQUESTED, {})


def _emit(outcome: str, change: str, detail: dict | None = None) -> int:
    """Print the machine-readable JSON (`{outcome, change, detail?, contract_version}`) and
    return the mapped exit code."""
    payload = {"outcome": outcome, "change": change}
    if detail and detail.get("detail"):
        payload["detail"] = detail.get("detail")
    payload["contract_version"] = CONTRACT_VERSION
    sys.stdout.write(json.dumps(payload) + "\n")
    return exit_code_for(outcome)


def build_parser() -> argparse.ArgumentParser:
    """`land <change> [--wait]` and `land-status <change>`; `--help` cites docs/land-contract.md."""
    parser = argparse.ArgumentParser(
        prog="land",
        description="Agent landing contract — one call, one typed outcome + exit code.",
        epilog="See docs/land-contract.md for the full outcome/exit-code contract.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_land = sub.add_parser(
        "land",
        help="request a land and (with --wait) block until terminal",
        epilog="See docs/land-contract.md.",
    )
    p_land.add_argument("change", help="the Gerrit change id/number")
    p_land.add_argument("--wait", action="store_true", help="block until a terminal outcome")
    p_status = sub.add_parser(
        "land-status",
        help="single-shot status read (never sets Autosubmit)",
        epilog="See docs/land-contract.md.",
    )
    p_status.add_argument("change", help="the Gerrit change id/number")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry: wire the real seams (GerritClient with the agent's ambient credential, the S5
    status endpoint reader, the S3 MarkerStore) and dispatch to land / land-status."""
    from autolander.failure import MarkerStore
    from autolander.gerrit import GerritClient

    args = build_parser().parse_args(argv)
    change = args.change

    base_url = os.environ.get("REBAR_GERRIT_URL", "https://rebar.solutions.navateam.com/a")
    user, token = _ambient_credential(base_url)
    gerrit = GerritClient(base_url, user, token)

    status_url = os.environ.get("REBAR_AUTOLANDER_STATUS_URL", "")

    def status_reader() -> dict:
        with urllib.request.urlopen(status_url, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8", "replace"))

    state_dir = os.environ.get("REBAR_AUTOLANDER_STATE_DIR", ".rebar/autolander-state")
    store = MarkerStore(state_dir)

    def marker_lookup(c: str):
        try:
            return store.get_valid(gerrit, c)
        except Exception as exc:  # noqa: BLE001 — a marker read failure must not crash the CLI
            sys.stderr.write(f"marker lookup failed for {c}: {exc}\n")
            return None

    def set_autosubmit(c: str) -> None:
        gerrit.set_review(c, labels={"Autosubmit": 1})

    if args.command == "land-status":
        outcome, detail = land_status(
            change,
            gerrit=gerrit,
            status_reader=status_reader,
            marker_lookup=marker_lookup,
        )
        return _emit(outcome, change, detail)

    if not args.wait:
        # A land request without --wait still needs the terminal contract; the loop returns as
        # soon as an outcome is known. (The heartbeat-first / Autosubmit semantics are in land.)
        pass
    outcome, detail = land(
        change,
        gerrit=gerrit,
        status_reader=status_reader,
        marker_lookup=marker_lookup,
        set_autosubmit=set_autosubmit,
        clock=time.monotonic,
        sleep=time.sleep,
        timeout_s=(WAIT_TIMEOUT_S if args.wait else 0),
    )
    return _emit(outcome, change, detail)


def _ambient_credential(base_url: str) -> tuple[str, str]:
    """Best-effort read of the agent's ambient Gerrit credential (git-credential, then
    .netrc). Returns ("", "") when none is found — the request will then 401 and surface as a
    transport error rather than crashing here."""
    from urllib.parse import urlsplit

    host = urlsplit(base_url).hostname or ""
    # git credential fill (honours the osxkeychain / store helpers)
    try:
        import subprocess

        query = f"protocol=https\nhost={host}\n\n"
        out = subprocess.run(  # noqa: S603
            ["git", "credential", "fill"],  # noqa: S607
            input=query,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
        creds = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        if creds.get("username") and creds.get("password"):
            return creds["username"], creds["password"]
    except Exception as exc:  # noqa: BLE001 — fall through to .netrc
        sys.stderr.write(f"git-credential lookup failed: {exc}\n")
    # .netrc fallback
    try:
        import netrc

        auth = netrc.netrc().authenticators(host)
        if auth:
            login, _account, password = auth
            return login or "", password or ""
    except Exception as exc:  # noqa: BLE001 — no credential available
        sys.stderr.write(f".netrc lookup failed: {exc}\n")
    return "", ""


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
