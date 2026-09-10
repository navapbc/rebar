#!/usr/bin/env python3
"""Render the scheduled ``main`` branch-health summary.

Gerrit verifies each patchset before submission, while the GitHub mirror checks the
combined branch on a schedule. A green report records the head SHA as the next lower
bound. A red report lists job results, resolves the newest successful run, and provides
the repository CI command for ``git bisect``. Failed lookups remain visible and never
fail the reporting job. An injected ``Runner`` keeps API failure paths testable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Mapping

# Injectable command result with return code, stdout, and stderr.
Runner = Callable[[list[str]], tuple[int, str, str]]

# Reproduce the scheduled gate from the committed lock at each bisect step.
BISECT_PAYLOAD = (
    "uv sync --locked --extra dev "
    '&& PATH="$PWD/.venv/bin:$PATH" make check '
    '&& PATH="$PWD/.venv/bin:$PATH" make test'
)

# Keep an unresolved lower bound visible instead of inventing a green commit.
NO_LOWER_BOUND = "<a commit you know was green>"


def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run the injected read-only ``gh api`` query and return its three result fields."""
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
    """Return the newest successful run or empty fields when lookup data is unusable.

    API failures, empty history, and malformed responses fail softly. The caller displays
    the missing lower bound instead of inventing one.
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
    """Render Markdown and require every gating job to report ``success`` for green."""
    if not jobs:
        return _render_unavailable(ref_name=ref_name, head_sha=head_sha, jobs=jobs)
    if all(result == "success" for result in jobs.values()):
        return _render_green(ref_name=ref_name, head_sha=head_sha)
    if all(result != "failure" for result in jobs.values()):
        return _render_unavailable(ref_name=ref_name, head_sha=head_sha, jobs=jobs)
    return _render_red(
        ref_name=ref_name,
        head_sha=head_sha,
        jobs=jobs,
        last_green_sha=last_green_sha,
        last_green_url=last_green_url,
    )


def _render_unavailable(*, ref_name: str, head_sha: str, jobs: dict[str, str]) -> str:
    lines = [
        f"## `{ref_name}` health is UNAVAILABLE at `{head_sha}`",
        "",
        "This run did not produce a complete branch-health reading. Treat it as neither",
        "healthy nor unhealthy; rerun the scheduled or manual health lane to obtain a verdict.",
    ]
    if jobs:
        lines += ["", "| job | result |", "| --- | --- |"]
        lines += [f"| {name} | {result} |" for name, result in sorted(jobs.items())]
    return "\n".join(lines)


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
            "no lower bound could be resolved from this workflow's run history — pick one by hand"
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
