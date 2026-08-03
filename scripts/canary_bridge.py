"""canary_bridge.py — extracted alert-classification logic from reconcile-bridge-canary.yml.

This module is Tier 3 of the shell→Python strangler-fig migration (ticket e602-1354-6778-4c0f).
It absorbs the four YAML run-block decision trees while deliberately leaving two loops in YAML:

- The reconcile-bridge.yml commit-back CAS push loop
- The canary flush (tickets-branch push retry loop)

Both loops require verbatim-execution guarantees per ticket 4c4f's decision; this module must
never absorb them.  The raw-git-write lint (d37e) allowlists those YAML loops specifically.
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

# Every external command (gh, rebar) goes through a Runner so unit tests can
# inject a fake: argv -> (returncode, stdout, stderr).
Runner = Callable[[list[str]], tuple[int, str, str]]

# ---------------------------------------------------------------------------
# Runner default
# ---------------------------------------------------------------------------


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(argv, capture_output=True, text=True)
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


def _find_alert_ticket(
    runner: Runner,
    tag: str,
) -> str:
    """Fail-soft: returns ticket_id string or '' on any error."""
    rc, stdout, _stderr = runner(
        ["rebar", "list", "--type=bug", "--status=open", f"--has-tag={tag}", "--output", "json"]
    )
    if rc != 0:
        return ""
    try:
        tickets = json.loads(stdout)
        return tickets[0]["ticket_id"] if tickets else ""
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return ""


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
) -> str:
    return textwrap.dedent(f"""\
        ## Reproduction Steps

        The Reconciler Heartbeat Canary detected that reconcile-bridge.yml has not
        completed a successful run within the {window_hours}-hour window
        (cron: */20). Inspect the Actions tab.

        ## Expected vs Actual

        - **Expected:** reconcile-bridge.yml completes a successful run at least once
          every {window_hours}h.
        - **Actual:** {status_msg}
        - **Detected at:** {detected_at}
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

    tid = _find_alert_ticket(runner, tag)
    ts = _utc_ts(now_epoch)

    if stale == "true" and not tid:
        title = (
            f"[heartbeat-canary] reconcile-bridge stale ({last_run_ago})"
            f" \u2014 no success within {window_hours}h"
        )
        desc = _heartbeat_description(last_run_ago, window_hours, status_msg, ts, run_url)
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
            ]
        )
        if rc != 0:
            print(stderr)
            return rc
    elif stale == "true" and tid:
        body = f"BRIDGE_CANARY_ALERT: Still stale as of {ts}: {status_msg} Run: {run_url}"
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
                f"--force-close={force_close}",
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
    "dangling",
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

    _rc, stdout, _stderr = runner(["rebar", "bridge-fsck", "--output", "json"])

    try:
        data: dict = json.loads(stdout) if stdout.strip() else {}
    except (json.JSONDecodeError, ValueError):
        data = {}

    bd = data.get("binding_drift") or {}
    counts = {c: len(bd.get(c, [])) for c in _DRIFT_CELLS}
    total = sum(counts.values())
    summary = ", ".join(f"{c}={counts[c]}" for c in _DRIFT_CELLS if counts[c]) or "none"

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

        Run `rebar bridge-fsck` — the offline binding-drift arm (epic 3006-e198,
        the convergence parity oracle) reports a non-empty `binding_drift` section.

        ## Expected vs Actual

        - **Expected:** `binding_drift` is empty (every bound pair converged).
        - **Actual:** {drift_summary}
        - **Detected at:** {detected_at}
        - **Canary run:** {run_url}

        ## Acceptance Criteria

        - [ ] Each reported drift is triaged (would_terminal → local archive/delete
              to propagate; dangling → confirmed 404 GC; unbound_jira → adopt or
              archive the Jira-native issue; local_gone → investigate).
        - [ ] `rebar bridge-fsck` reports an empty `binding_drift` section.

        This ticket auto-closes when bridge-fsck next reports zero binding drift.""")


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

    tid = _find_alert_ticket(runner, _DRIFT_TAG)

    if drift_found == "true" and not tid:
        title = f"[binding-drift] bridge-fsck found {drift_total} unhealed binding drift(s)"
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
            ]
        )
        if rc != 0:
            print(stderr)
            return rc
    elif drift_found == "true" and tid:
        body = (
            f"BRIDGE_CANARY_ALERT: Binding drift still present as of {ts}:"
            f" {drift_summary}. Run: {run_url}"
        )
        rc, _out, stderr = runner(["rebar", "comment", tid, body])
        if rc != 0:
            print(stderr)
            return rc
    elif drift_found == "false" and tid:
        reason = f"Fixed: bridge-fsck reports zero binding drift at {ts}."
        force_close = f"Fixed: zero binding drift at {ts} (bot alert auto-close)."
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
                f"--force-close={force_close}",
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
        sub.add_parser(name)

    args = parser.parse_args(argv)
    if args.subcommand is None:
        parser.print_help()
        return 1

    fn = _SUBCOMMANDS[args.subcommand]
    return fn(args, runner, environ, now_epoch)


if __name__ == "__main__":
    sys.exit(main())
