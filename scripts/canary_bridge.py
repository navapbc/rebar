"""canary_bridge.py — extracted alert-classification logic from reconcile-bridge-canary.yml.

This module is Tier 3 of the shell→Python strangler-fig migration (ticket e602-1354-6778-4c0f).
It absorbs the four YAML run-block decision trees. Tickets-branch commit and delivery now live
in ``rebar._store.push``; the workflows retain only strict process-boundary adapters.

The two alert subcommands are automated bug filers and follow the bug-creation
contract — dedup search first, ≤1 accumulation comment per 24h, abort-if-empty,
consecutive-red threshold (heartbeat only; drift is persistent state), and
--detected-by provenance.  See docs/bug-creation-contract.md (ticket 4527).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Mapping
from pathlib import Path

# `alert_dedup` is a SIBLING module in this directory, not an installed package. Under
# `python scripts/canary_bridge.py` it resolves because the script's directory leads
# sys.path, but a test that loads this file via `importlib.util.spec_from_file_location`
# gets no such entry — so the bare import raised ModuleNotFoundError whenever the loading
# test ran without something else having inserted `scripts/` first, which is exactly what a
# subset run does (bug 291e-7b48-3f24-41c6). Derive the directory from `__file__` so
# resolution is invocation-independent; the membership check keeps it idempotent and never
# reorders an entry that is already present.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import alert_dedup  # noqa: E402  (needs _SCRIPTS_DIR on sys.path, set just above)

# Every external command (gh, rebar) goes through a Runner so unit tests can
# inject a fake: argv -> (returncode, stdout, stderr).
Runner = Callable[[list[str]], tuple[int, str, str]]

# ---------------------------------------------------------------------------
# Runner default
# ---------------------------------------------------------------------------


# raw-git-ok: generic command runner, argv supplied by caller
def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_ts(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _append_outputs(path: str, **kv: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


# Dedup (find-the-open-alert + cap accumulation at 1 comment/24h) is SHARED with the
# dependency-advisory filer and lives in scripts/alert_dedup.py (ticket 63e8). These two
# names stay as this module's spelling of it — the marker is what differs per lane.
_find_alert_ticket = alert_dedup.find_alert_ticket

_ALERT_MARKER = "BRIDGE_CANARY_ALERT:"
_ACCUMULATION_WINDOW_SECS = alert_dedup.ACCUMULATION_WINDOW_SECS


def _recent_marker_comment(runner: Runner, tid: str, now_epoch: int) -> bool:
    """True if the ticket already carries a canary marker comment younger than 24h."""
    return alert_dedup.recent_marker_comment(
        runner, tid, _ALERT_MARKER, now_epoch, _ACCUMULATION_WINDOW_SECS
    )


def _previous_canary_red(
    runner: Runner,
    environ: Mapping[str, str],
) -> tuple[bool, str]:
    """(previous completed canary run was red, its updated_at ISO timestamp).

    Consecutive-red threshold for the heartbeat filer: a ticket is only filed
    when the *previous* completed canary run also failed, so a single flake
    (API blip, runner hiccup) never opens a bug. Fails toward NOT filing —
    a query error logs loudly and reports "not red" (the next canary cycle
    retries; a missed cycle only delays the alert by one cron period).
    """
    repo = environ.get("GITHUB_REPOSITORY", "")
    workflow = environ.get("CANARY_WORKFLOW_FILE", "")
    current_run = environ.get("GITHUB_RUN_ID", "")
    if not repo or not workflow:
        print(
            "::warning::GITHUB_REPOSITORY/CANARY_WORKFLOW_FILE unset —"
            " cannot check run history, treating as first red (no ticket)."
        )
        return False, ""
    rc, stdout, stderr = runner(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/workflows/{workflow}/runs?status=completed&per_page=5",
            "--jq",
            "[.workflow_runs[] | {id, conclusion, updated_at}]",
        ]
    )
    if rc != 0:
        print(
            f"::warning::canary run-history query failed (exit {rc}): {stderr}"
            " — treating as first red, no ticket this cycle."
        )
        return False, ""
    try:
        runs = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        print("::warning::canary run-history unparseable — treating as first red.")
        return False, ""
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, dict) or str(run.get("id", "")) == current_run:
            continue
        if run.get("conclusion") == "failure":
            return True, str(run.get("updated_at") or "")
        return False, ""  # most recent prior completed run was not red
    return False, ""


# ---------------------------------------------------------------------------
# Subcommand: check-heartbeat
# ---------------------------------------------------------------------------


def cmd_check_heartbeat(
    args: argparse.Namespace,
    runner: Runner,
    environ: Mapping[str, str],
    now_epoch: int,
) -> int:
    window_str = environ.get("ALERT_WINDOW_HOURS", "")
    if not re.fullmatch(r"[1-9][0-9]*", window_str):
        print(f"::error::alert_window_hours must be a positive integer, got: '{window_str}'")
        return 1

    window_hours = int(window_str)
    source = getattr(args, "source", None) or environ.get("REBAR_CANARY_HEARTBEAT_SOURCE", "status")
    if source not in {"status", "github-api"}:
        print(
            f"::error::REBAR_CANARY_HEARTBEAT_SOURCE must be status or github-api, got {source!r}"
        )
        return 1
    if source == "status":
        return _check_status_heartbeat(runner, environ, now_epoch, window_hours)
    return _check_github_heartbeat(runner, environ, now_epoch, window_hours)


def _check_status_heartbeat(
    runner: Runner,
    environ: Mapping[str, str],
    now_epoch: int,
    window_hours: int,
) -> int:
    """Classify the canonical bridge status, including the stalled-lease witness."""
    gh_output = environ["GITHUB_OUTPUT"]

    def take_snapshot() -> dict | None:
        rc, stdout, stderr = runner(
            [
                "rebar",
                "bridge",
                "status",
                "--target",
                "reconciler",
                "--max-age",
                f"{window_hours}h",
                "--json",
            ]
        )
        try:
            decoded = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            print(f"::error::bridge status returned invalid JSON (exit {rc}): {stderr}")
            return None
        if not isinstance(decoded, dict) or not isinstance(decoded.get("verdict"), str):
            print("::error::bridge status JSON is missing a verdict")
            return None
        return decoded

    status = take_snapshot()
    if status is None:
        return 1
    verdict = status["verdict"]
    if verdict == "NEVER_RUN":
        # Producer-first rollout: old producers have no ref yet, so retain the
        # one-release GitHub run-history witness until the first new pass lands.
        return _check_github_heartbeat(
            runner,
            environ,
            now_epoch,
            window_hours,
            bootstrap=True,
        )
    if verdict == "RUNNING":
        lease = status.get("lock_lease_secs")
        if not isinstance(lease, (int, float)) or isinstance(lease, bool) or lease <= 0:
            print("::error::RUNNING status omitted a positive lock lease")
            return 1
        if lease > 480:
            print(
                f"::error::reconciler lease {lease:g}s exceeds the 480s canary ceiling "
                "inside the fixed 10-minute job timeout"
            )
            return 1
        time.sleep(lease)
        second = take_snapshot()
        if second is None:
            return 1
        advanced = second.get("verdict") == "RUNNING" and (
            second.get("lock_oid") != status.get("lock_oid")
            or second.get("live_lock_fence") != status.get("live_lock_fence")
        )
        if advanced:
            _append_outputs(
                gh_output,
                stale="false",
                last_run_ago="in progress",
                status_msg="Reconciler healthy — live lease advanced during the canary witness.",
            )
            return 0
        if second.get("verdict") != "RUNNING":
            second_verdict = str(second.get("verdict") or "INDETERMINATE")
            recovered = second_verdict in {"HEALTHY", "PAUSED"}
            _append_outputs(
                gh_output,
                stale="false" if recovered else "true",
                last_run_ago=str(second.get("completed_at") or "never"),
                status_msg=f"Reconciler status {second_verdict} after the observed lease.",
            )
            return 0
        _append_outputs(
            gh_output,
            stale="true",
            last_run_ago="crashed",
            status_msg="CRASHED — reconciler lease made no OID/fence progress over one lease.",
        )
        return 0
    healthy = verdict in {"HEALTHY", "PAUSED"}
    _append_outputs(
        gh_output,
        stale="false" if healthy else "true",
        last_run_ago=str(status.get("completed_at") or "never"),
        status_msg=(
            f"Reconciler status {verdict}."
            if healthy
            else f"Reconciler unhealthy — canonical status is {verdict}."
        ),
    )
    return 0


def _check_github_heartbeat(
    runner: Runner,
    environ: Mapping[str, str],
    now_epoch: int,
    window_hours: int,
    *,
    bootstrap: bool = False,
) -> int:
    """One-release rollback/bootstrap source based on successful workflow runs."""
    repo = environ["GITHUB_REPOSITORY"]
    gh_output = environ["GITHUB_OUTPUT"]

    rc, stdout, stderr = runner(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/workflows/reconcile-bridge.yml/runs?status=success&per_page=1",
            "--jq",
            ".workflow_runs[0] // empty",
        ]
    )

    if rc != 0:
        print(
            f"::warning::GitHub Actions API error (exit {rc}): {stderr}"
            " — treating as transient, no alert this cycle."
        )
        _append_outputs(
            gh_output,
            stale="false",
            last_run_ago="unknown",
            status_msg="GitHub Actions API error — heartbeat indeterminate, treating as transient.",
        )
        return 0

    if not stdout.strip():
        _append_outputs(
            gh_output,
            stale="true",
            last_run_ago="never",
            status_msg="No successful reconcile-bridge.yml runs found.",
        )
        return 0

    data = json.loads(stdout)
    updated_at: str = data["updated_at"]
    run_epoch = int(
        datetime.datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=datetime.timezone.utc)
        .timestamp()
    )
    age_secs = now_epoch - run_epoch
    age_hours = age_secs // 3600
    age_mins = (age_secs % 3600) // 60
    ago = f"{age_hours}h {age_mins}m ago"

    cutoff = now_epoch - window_hours * 3600
    if run_epoch < cutoff:
        _append_outputs(
            gh_output,
            stale="true",
            last_run_ago=ago,
            status_msg=(
                f"Last successful run was {age_hours}h {age_mins}m ago"
                f" (threshold: {window_hours}h)."
            ),
        )
    else:
        _append_outputs(
            gh_output,
            stale="false",
            last_run_ago=ago,
            status_msg=(
                f"Reconciler healthy \u2014 last successful run was {age_hours}h {age_mins}m ago."
                + (" (last-pass producer bootstrap fallback)." if bootstrap else "")
            ),
        )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: heartbeat-alert
# ---------------------------------------------------------------------------


def _heartbeat_description(
    last_run_ago: str,
    window_hours: str,
    status_msg: str,
    detected_at: str,
    run_url: str,
    first_red_at: str,
    workflow_file: str,
) -> str:
    return textwrap.dedent(f"""\
        ## Reproduction Steps

        The Reconciler Heartbeat Canary ({workflow_file}) detected that
        reconcile-bridge.yml has not completed a successful run within the
        {window_hours}-hour window (bridge cadence: hourly). Inspect the Actions tab.

        ## Expected vs Actual

        - **Expected:** reconcile-bridge.yml completes a successful run at least once
          every {window_hours}h.
        - **Actual:** {status_msg}
        - **First red canary run:** {first_red_at or "unknown"}
        - **Detected at:** {detected_at} (second consecutive red canary run)
        - **Canary run:** {run_url}

        ## Acceptance Criteria

        - [ ] reconcile-bridge.yml is running and its most recent run succeeded.
        - [ ] Root cause identified (workflow disabled / runner outage / acli auth /
              Jira creds / reconciler error) and addressed.

        This ticket auto-closes when the next successful reconcile-bridge run is detected.""")


def cmd_heartbeat_alert(
    args: argparse.Namespace,
    runner: Runner,
    environ: Mapping[str, str],
    now_epoch: int,
) -> int:
    if environ.get("DRY_RUN") == "true":
        return 0

    tag = environ["ALERT_TAG"]
    window_hours = environ.get("ALERT_WINDOW_HOURS", "")
    stale = environ.get("STALE", "false")
    last_run_ago = environ.get("LAST_RUN_AGO", "")
    status_msg = environ.get("STATUS_MSG", "")
    run_url = environ.get("RUN_URL", "")

    if stale == "true" and not status_msg.strip():
        print(
            "::error::heartbeat-alert invoked stale with an empty STATUS_MSG —"
            " refusing to file a hollow ticket. Fix the check-heartbeat wiring."
        )
        return 1

    tid = _find_alert_ticket(runner, tag)
    ts = _utc_ts(now_epoch)

    if stale == "true" and not tid:
        prev_red, first_red_at = _previous_canary_red(runner, environ)
        if not prev_red:
            print(
                "::warning::heartbeat stale but previous canary run was not red —"
                " first red, holding off (threshold: 2 consecutive red runs)."
            )
            return 0
        title = (
            f"[heartbeat-canary] reconcile-bridge stale ({last_run_ago})"
            f" \u2014 no success within {window_hours}h"
        )
        desc = _heartbeat_description(
            last_run_ago,
            window_hours,
            status_msg,
            ts,
            run_url,
            first_red_at,
            environ.get("CANARY_WORKFLOW_FILE", ""),
        )
        rc, _out, stderr = runner(
            [
                "rebar",
                "create",
                "bug",
                title,
                "--priority",
                "1",
                "--tags",
                tag,
                "--description",
                desc,
                "--detected-by",
                "heartbeat-canary",
            ]
        )
        if rc != 0:
            print(stderr)
            return rc
    elif stale == "true" and tid:
        if _recent_marker_comment(runner, tid, now_epoch):
            print(f"Alert ticket {tid} already has a marker comment <24h old — skipping.")
            return 0
        body = f"{_ALERT_MARKER} Still stale as of {ts}: {status_msg} Run: {run_url}"
        rc, _out, stderr = runner(["rebar", "comment", tid, body])
        if rc != 0:
            print(stderr)
            return rc
    elif stale == "false" and tid:
        reason = f"Fixed: reconciler recovered at {ts}. {status_msg}"
        force_close = (
            f"Fixed: reconciler recovered at {ts}"
            " (bot alert auto-close; heartbeat tickets have no completion criteria to verify)."
        )
        rc, _out, stderr = runner(
            [
                "rebar",
                "transition",
                tid,
                "open",
                "closed",
                "--class",
                "env_integration",
                "--reason",
                reason,
                f"--force={force_close}",
            ]
        )
        if rc != 0:
            print(stderr)
            return rc

    return 0


# ---------------------------------------------------------------------------
# Subcommand: check-binding-drift
# ---------------------------------------------------------------------------

_DRIFT_CELLS = (
    "would_terminal",
    "local_gone",
    "unbound_jira",
    "retired_overlap",
)


def cmd_check_binding_drift(
    args: argparse.Namespace,
    runner: Runner,
    environ: Mapping[str, str],
    now_epoch: int,
) -> int:
    gh_output = environ["GITHUB_OUTPUT"]

    rc, stdout, stderr = runner(["rebar", "bridge", "fsck", "--output", "json"])

    # Exit 1 is the expected findings signal and still carries the JSON report.
    # Exit 2 is an operational scan failure; never reinterpret it as an empty
    # report, which could incorrectly close an existing alert ticket.
    if rc not in {0, 1}:
        print(f"::error::bridge fsck failed operationally (exit {rc}): {stderr or 'no diagnostic'}")
        return 1

    try:
        decoded = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        print("::error::bridge fsck returned invalid JSON")
        return 1
    if not isinstance(decoded, dict) or not {
        "unknown_event_types",
        "binding_drift",
        "store_integrity",
    }.issubset(decoded):
        print("::error::bridge fsck JSON is missing the required audit fields")
        return 1
    data: dict = decoded

    bd = data.get("binding_drift") or {}
    counts = {c: len(bd.get(c, [])) for c in _DRIFT_CELLS}
    integrity_count = len(data.get("store_integrity") or [])
    total = sum(counts.values()) + integrity_count
    summary_parts = [f"{c}={counts[c]}" for c in _DRIFT_CELLS if counts[c]]
    if integrity_count:
        summary_parts.append(f"store_integrity={integrity_count}")
    summary = ", ".join(summary_parts) or "none"

    _append_outputs(
        gh_output,
        drift_found="true" if total else "false",
        drift_total=str(total),
        drift_summary=summary,
    )
    return 0


# ---------------------------------------------------------------------------
# Subcommand: binding-drift-alert
# ---------------------------------------------------------------------------

_DRIFT_TAG = "binding-drift-alert"


def _drift_description(
    drift_total: str,
    drift_summary: str,
    detected_at: str,
    run_url: str,
) -> str:
    return textwrap.dedent(f"""\
        ## Reproduction Steps

        Run `rebar bridge fsck` — the offline bridge audit reports alerting binding
        drift or a forward/reverse binding-store integrity inconsistency.

        ## Expected vs Actual

        - **Expected:** alerting `binding_drift` cells and `store_integrity` are empty.
        - **Actual:** {drift_summary}
        - **Detected at:** {detected_at}
        - **Canary run:** {run_url}

        ## Acceptance Criteria

        - [ ] Each reported drift is triaged (would_terminal → local archive/delete
              to propagate; unbound_jira → adopt or archive the Jira-native issue;
              local_gone → investigate; retired_overlap → remove the duplicate live/
              retired membership; store_integrity → repair the inconsistent forward/
              reverse index entries).
        - [ ] `rebar bridge fsck` reports empty alerting `binding_drift` cells and
              `store_integrity`.

        This ticket auto-closes when bridge fsck next reports zero alerting audit findings.""")


def cmd_binding_drift_alert(
    args: argparse.Namespace,
    runner: Runner,
    environ: Mapping[str, str],
    now_epoch: int,
) -> int:
    if environ.get("DRY_RUN") == "true":
        return 0

    drift_found = environ.get("DRIFT_FOUND", "false")
    drift_total = environ.get("DRIFT_TOTAL", "0")
    drift_summary = environ.get("DRIFT_SUMMARY", "none")
    run_url = environ.get("RUN_URL", "")
    ts = _utc_ts(now_epoch)

    if drift_found == "true" and (not drift_summary.strip() or drift_summary.strip() == "none"):
        print(
            "::error::binding-drift-alert invoked with drift_found=true but an"
            " empty/none summary — refusing to file a hollow ticket."
        )
        return 1

    tid = _find_alert_ticket(runner, _DRIFT_TAG)

    # No consecutive-red threshold here (unlike heartbeat-alert): binding drift is
    # persistent store state that cannot self-heal between runs, and the fsck
    # oracle never fails the canary run, so run conclusions carry no drift signal.
    if drift_found == "true" and not tid:
        title = f"[binding-drift] bridge fsck found {drift_total} audit finding(s)"
        desc = _drift_description(drift_total, drift_summary, ts, run_url)
        rc, _out, stderr = runner(
            [
                "rebar",
                "create",
                "bug",
                title,
                "--priority",
                "1",
                "--tags",
                _DRIFT_TAG,
                "--description",
                desc,
                "--detected-by",
                "binding-drift-canary",
            ]
        )
        if rc != 0:
            print(stderr)
            return rc
    elif drift_found == "true" and tid:
        if _recent_marker_comment(runner, tid, now_epoch):
            print(f"Drift ticket {tid} already has a marker comment <24h old — skipping.")
            return 0
        body = (
            f"{_ALERT_MARKER} Bridge audit findings still present as of {ts}:"
            f" {drift_summary}. Run: {run_url}"
        )
        rc, _out, stderr = runner(["rebar", "comment", tid, body])
        if rc != 0:
            print(stderr)
            return rc
    elif drift_found == "false" and tid:
        reason = f"Fixed: bridge fsck reports zero audit findings at {ts}."
        force_close = f"Fixed: zero bridge audit findings at {ts} (bot alert auto-close)."
        rc, _out, stderr = runner(
            [
                "rebar",
                "transition",
                tid,
                "open",
                "closed",
                "--class",
                "env_integration",
                "--reason",
                reason,
                f"--force={force_close}",
            ]
        )
        if rc != 0:
            print(stderr)
            return rc

    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

_SUBCOMMANDS = {
    "check-heartbeat": cmd_check_heartbeat,
    "heartbeat-alert": cmd_heartbeat_alert,
    "check-binding-drift": cmd_check_binding_drift,
    "binding-drift-alert": cmd_binding_drift_alert,
}


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner | None = None,
    environ: Mapping[str, str] | None = None,
    now_epoch: int | None = None,
) -> int:
    if runner is None:
        runner = _default_runner
    if environ is None:
        environ = os.environ
    if now_epoch is None:
        now_epoch = int(time.time())

    parser = argparse.ArgumentParser(
        description="Canary bridge: extracted alert logic from reconcile-bridge-canary.yml"
    )
    sub = parser.add_subparsers(dest="subcommand")
    for name in _SUBCOMMANDS:
        child = sub.add_parser(name)
        if name == "check-heartbeat":
            child.add_argument(
                "--source",
                choices=("status", "github-api"),
                default=None,
                help="Heartbeat source (default: canonical bridge status).",
            )

    args = parser.parse_args(argv)
    if args.subcommand is None:
        parser.print_help()
        return 1

    fn = _SUBCOMMANDS[args.subcommand]
    return fn(args, runner, environ, now_epoch)


if __name__ == "__main__":
    sys.exit(main())
