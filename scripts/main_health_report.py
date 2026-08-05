#!/usr/bin/env python3
"""Render the branch-health run summary for the scheduled `main` CI lane.

Branch CI on the GitHub mirror runs on a SCHEDULE rather than on every push to `main`
(ticket 03ef-6fb5-158b-4abd): Gerrit's `Verified` gate already tests every patchset before it
lands, so the mirror's job is to answer "is `main` healthy NOW", not "was every commit green".
Running it per-push meant a ref-keyed, cancel-in-progress concurrency group cancelled each run
with the next, and GitHub renders a cancelled run as a red X — a healthy `main` looked broken.

The schedule buys that back at the cost of ATTRIBUTION: one red tick now covers every commit
since the last green one. This module is how that cost is paid back. It renders:

* on GREEN — the head SHA, named as the new last-known-green lower bound, so the value is
  discoverable before an incident rather than only during one;
* on RED — the per-job verdicts, the last-known-green SHA resolved from this workflow's own
  successful run history, and a copy-pasteable ``git bisect start``/``git bisect run`` pair
  wired to this repo's own CI reproduction, so the first responder does not have to
  reconstruct the technique under pressure.

It lives here rather than inline in the workflow so the suite can drive it: workflow YAML is
not reachable from a test, and a shell-quoting bug in a recipe nobody has exercised — or a
lower-bound lookup that silently 403s — is exactly the failure that only shows up at 2am. The
GitHub API call is injected as a ``Runner`` (the pattern ``scripts/canary_bridge.py`` uses) so
every failure mode is exercised without a network.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping

# (argv) -> (returncode, stdout, stderr) — the seam the tests replace.
Runner = Callable[[list[str]], tuple[int, str, str]]

# The reproduction the responder runs at each bisect step. It mirrors what CI does — install
# from the committed lock, then the check-only gates, then the default suite — so a bisect
# converges on the same verdict the scheduled run produced.
BISECT_PAYLOAD = (
    "uv sync --locked --extra dev "
    '&& PATH="$PWD/.venv/bin:$PATH" make check '
    '&& PATH="$PWD/.venv/bin:$PATH" make test'
)

# Shown in place of a SHA when the lower bound could not be established. It is a VISIBLE
# placeholder on purpose: a plausible-but-wrong bound would send the bisect through commits
# that were never green.
NO_LOWER_BOUND = "<a commit you know was green>"


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run ``argv`` and return ``(returncode, stdout, stderr)``.

    ``argv`` is opaque to the raw-git-write gate only because this is the INJECTABLE seam;
    its sole caller is :func:`resolve_last_green`, which builds exactly one command — a
    read-only ``gh api .../actions/workflows/<file>/runs`` REST query. This module invokes no
    git subcommand at all and never touches the ticket store: it runs on a CI runner, reads
    one endpoint, and writes Markdown to stdout.
    """
    proc = subprocess.run(  # raw-git-ok: read-only `gh api` seam; never a git subcommand
        argv, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout, proc.stderr


def resolve_last_green(
    runner: Runner,
    *,
    repo: str,
    workflow_file: str,
    branch: str,
) -> tuple[str, str]:
    """Return ``(sha, run_url)`` for the newest successful run of this workflow on ``branch``.

    FAIL-SOFT BY CONTRACT. Every failure mode — the API call erroring (403 from a missing
    ``actions: read`` scope, 5xx, rate limit, timeout), an empty run history, or a malformed
    body — returns ``("", "")``. The caller renders a visible placeholder instead. A report
    describing a failure must never itself fail the run, and must never invent a bound.
    """
    query = f"branch={branch}&status=success&per_page=1"
    returncode, stdout, stderr = runner(
        ["gh", "api", f"repos/{repo}/actions/workflows/{workflow_file}/runs?{query}"]
    )
    if returncode != 0:
        sys.stderr.write(
            f"::warning::could not resolve the last known-green run (gh exit {returncode}): "
            f"{stderr.strip()}\n"
        )
        return "", ""
    try:
        payload = json.loads(stdout)
        runs = payload["workflow_runs"]
    except (ValueError, KeyError, TypeError):
        sys.stderr.write("::warning::unexpected Actions API response; no lower bound\n")
        return "", ""
    if not runs:
        return "", ""
    newest = runs[0]
    if not isinstance(newest, Mapping):
        return "", ""
    return str(newest.get("head_sha") or ""), str(newest.get("html_url") or "")


def render(
    *,
    ref_name: str,
    head_sha: str,
    jobs: dict[str, str],
    last_green_sha: str = "",
    last_green_url: str = "",
) -> str:
    """Return the Markdown run summary for one branch-health run.

    ``jobs`` maps each gating job's name to its GitHub result string (``success``,
    ``failure``, ``cancelled``, ``skipped``). The run is green only when every gating job
    succeeded, so a cancelled or skipped gate is treated as unproven, never as passing.
    """
    if jobs and all(result == "success" for result in jobs.values()):
        return _render_green(ref_name=ref_name, head_sha=head_sha)
    return _render_red(
        ref_name=ref_name,
        head_sha=head_sha,
        jobs=jobs,
        last_green_sha=last_green_sha,
        last_green_url=last_green_url,
    )


def _render_green(*, ref_name: str, head_sha: str) -> str:
    return "\n".join(
        [
            f"## `{ref_name}` is GREEN",
            "",
            f"Last known-green `{ref_name}`: `{head_sha}` (this run).",
            "",
            "Branch CI runs on a 6-hourly schedule, so this verdict covers every commit up to",
            "and including that SHA. Each of them also passed the Gerrit `Verified` gate",
            "individually before it landed.",
        ]
    )


def _render_red(
    *,
    ref_name: str,
    head_sha: str,
    jobs: dict[str, str],
    last_green_sha: str,
    last_green_url: str,
) -> str:
    if last_green_sha:
        good = last_green_sha
        provenance = (
            f"last known-green run: {last_green_url}"
            if last_green_url
            else "resolved from this workflow's own successful run history"
        )
    else:
        good = NO_LOWER_BOUND
        provenance = (
            "no lower bound could be resolved from this workflow's run history — pick one "
            "by hand"
        )

    lines = [f"## `{ref_name}` is RED at `{head_sha}`", ""]
    if jobs:
        lines += ["| job | result |", "| --- | --- |"]
        lines += [f"| {name} | {result} |" for name, result in sorted(jobs.items())]
        lines.append("")
    lines += [
        f"Last known-green: `{good}` — {provenance}",
        "",
        "Every commit in this window passed the Gerrit `Verified` gate on its own, so a red",
        "tick here is a semantic conflict between two independently-verified changes,",
        "dependency/environment drift, or a flake. Bisect the window with the same gates CI",
        "runs — log2(n) steps, typically 3-5 builds:",
        "",
        "```sh",
        f"git fetch origin {ref_name} && git checkout {ref_name}",
        f"git bisect start {head_sha} {good}",
        f"git bisect run sh -c '{BISECT_PAYLOAD}'",
        "git bisect reset",
        "```",
        "",
        "Narrow `make test` to the single failing test id for a much faster bisect.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the branch-health run summary.")
    parser.add_argument("--ref-name", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument(
        "--jobs",
        required=True,
        help='JSON object mapping gating job name to result, e.g. {"build-and-test":"success"}',
    )
    parser.add_argument("--repo", default="", help="owner/name; omit to skip the lookup")
    parser.add_argument("--workflow-file", default="test.yml")
    args = parser.parse_args(argv)

    try:
        jobs = json.loads(args.jobs)
    except ValueError:
        parser.error("--jobs must be valid JSON")
    if not isinstance(jobs, dict):
        parser.error("--jobs must be a JSON object")

    sha, url = "", ""
    if args.repo:
        sha, url = resolve_last_green(
            runner or _default_runner,
            repo=args.repo,
            workflow_file=args.workflow_file,
            branch=args.ref_name,
        )

    summary = render(
        ref_name=args.ref_name,
        head_sha=args.head_sha,
        jobs={str(k): str(v) for k, v in jobs.items()},
        last_green_sha=sha,
        last_green_url=url,
    )
    sys.stdout.write(summary + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
