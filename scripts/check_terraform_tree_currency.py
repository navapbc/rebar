#!/usr/bin/env python3
# mechanism-ok: ci_gate scripts/check_terraform_tree_currency.py — bug eebc-6aa1-e45b-4325:
# a terraform plan from a stale tree reports "No changes" indistinguishably from converged
# infrastructure; nothing in the existing surface can tell those two apart.
"""Assert a checkout is CURRENT with its tracked branch before terraform reasons about it.

Bug ``eebc-6aa1-e45b-4325`` (``jaded-pugnacious-isopod``). ``terraform plan`` answers
"does the live world match THIS TREE", but every reader hears "does the live world match the
declaration". Those diverge silently the moment the tree is behind its tracked branch:

* **Under-application** — observed 2026-09-05. A checkout 14 commits behind ``origin/main``
  planned ``0 to add, 0 to change, 0 to destroy`` while five CloudWatch alarms declared on
  ``main`` did not exist in AWS. The same plan from a tree at ``origin/main``, against the same
  remote state, returned ``5 to add, 1 to change``. The stale output is byte-identical to health.
* **Un-deletion** — the worse direction. A tree that PREDATES a commit removing a resource
  proposes RECREATING it, so a stale apply does not merely fail to converge: it actively undoes
  an intended deletion. Neither direction emits a warning.

The check is deliberately a GIT question, not a terraform one, and it is answered from a FRESH
fetch of the remote ref rather than from anything in the working tree. That is what keeps it
from being defeated by the very staleness it looks for: a tree missing the newest declarations
cannot also make the remote tip look like itself. It is plain Python + ``git``, with no CI
provider, no cloud call and no terraform binary, so a developer planning locally runs the exact
check the daily drift workflow runs (``project.portability``).

Usage::

    python scripts/check_terraform_tree_currency.py                  # HEAD must BE origin/main
    python scripts/check_terraform_tree_currency.py --mode ancestor  # HEAD must CONTAIN it
    python scripts/check_terraform_tree_currency.py --no-fetch       # offline; trust local refs

Exit codes are three-valued on purpose — "current", "stale" and "could not tell" are three
different facts, and collapsing the third into either of the others is how this class of bug
gets made:

===  ==========================================================================
  0  CURRENT — the tree is the tracked tip (``tip``) or contains it (``ancestor``)
  1  STALE — the tree is behind or diverged; a plan from it answers a stale question
  2  UNKNOWN — currency could not be established (no remote, fetch failed, no merge base)
===  ==========================================================================
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

EXIT_CURRENT = 0
EXIT_STALE = 1
EXIT_UNKNOWN = 2

DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
MODE_TIP = "tip"
MODE_ANCESTOR = "ancestor"
MODES = (MODE_TIP, MODE_ANCESTOR)

_GIT_TIMEOUT = 120

# Said in full at every stale exit. The point of the whole gate is that "no changes" from a
# stale tree is not evidence of convergence, so the verdict has to say so out loud.
_STALE_MEANING = (
    "A terraform plan from this tree answers a DIFFERENT question than "
    '"has the tracked branch been applied": resources declared on the branch but absent '
    'here read as "No changes", and resources DELETED on the branch but still present here '
    "are proposed for RECREATION."
)


class GitError(RuntimeError):
    """A git invocation failed; currency cannot be established from it."""


@dataclass(frozen=True)
class Verdict:
    """A three-valued currency verdict and the lines that explain it."""

    code: int
    lines: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        return self.code == EXIT_CURRENT


def git(repo: Path, *args: str) -> str:
    """Run ``git`` in ``repo``, returning stripped stdout or raising :class:`GitError`."""
    try:
        # raw-git-ok: read-only plumbing (rev-parse / merge-base / fetch) against an
        # ARBITRARY checkout the operator names, never the rebar tracker store. This gate
        # answers a question ABOUT a tree; it writes nothing to one.
        proc = subprocess.run(  # raw-git-ok: read-only plumbing against an arbitrary checkout
            ["git", *args],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env failure
        raise GitError(f"git {' '.join(args)}: {exc}") from exc
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def evaluate(
    *,
    head: str,
    tip: str,
    merge_base: str | None,
    mode: str,
    label: str,
) -> Verdict:
    """Classify a tree against its tracked tip. Pure: no git, no filesystem, no clock.

    ``merge_base`` is the merge base of ``head`` and ``tip``, or ``None`` when git could not
    compute one (unrelated histories, or a shallow clone that does not reach back far enough).
    An uncomputable merge base is UNKNOWN, never CURRENT.
    """
    if head == tip:
        return Verdict(EXIT_CURRENT, (f"terraform tree currency OK: HEAD is {label} ({head}).",))
    if mode == MODE_ANCESTOR:
        if merge_base is None:
            return Verdict(
                EXIT_UNKNOWN,
                (
                    f"terraform tree currency UNKNOWN: no merge base between HEAD ({head}) "
                    f"and {label} ({tip}).",
                    "Refusing to treat an unestablished currency as current.",
                ),
            )
        if merge_base == tip:
            return Verdict(
                EXIT_CURRENT,
                (f"terraform tree currency OK: HEAD ({head}) contains {label} ({tip}).",),
            )
    return Verdict(EXIT_STALE, _stale_lines(head=head, tip=tip, merge_base=merge_base, label=label))


def _stale_lines(*, head: str, tip: str, merge_base: str | None, label: str) -> tuple[str, ...]:
    relation = "behind" if merge_base == head else "diverged from"
    return (
        f"terraform tree currency STALE: HEAD ({head}) is {relation} {label} ({tip}).",
        _STALE_MEANING,
        f"Fast-forward this tree (git fetch && git merge --ff-only {label}) or plan from a "
        "worktree at that ref, then re-run.",
    )


def resolve_tip(repo: Path, *, remote: str, branch: str, fetch: bool) -> tuple[str, str]:
    """``(tip sha, label)`` for the tracked branch, refreshed from the remote by default.

    Fetching first is what makes the check undefeatable by the stale tree: the answer comes
    from the remote, not from anything the tree happens to contain.
    """
    label = f"{remote}/{branch}"
    if fetch:
        git(repo, "fetch", "--quiet", remote, branch)
        return git(repo, "rev-parse", "FETCH_HEAD"), label
    return git(repo, "rev-parse", f"refs/remotes/{label}"), label


def merge_base(repo: Path, head: str, tip: str) -> str | None:
    try:
        return git(repo, "merge-base", head, tip)
    except GitError:
        return None


def check(repo: Path, *, remote: str, branch: str, mode: str, fetch: bool) -> Verdict:
    """Establish the tree's currency, or report that it could not be established."""
    try:
        head = git(repo, "rev-parse", "HEAD")
        tip, label = resolve_tip(repo, remote=remote, branch=branch, fetch=fetch)
    except GitError as exc:
        return Verdict(
            EXIT_UNKNOWN,
            (
                f"terraform tree currency UNKNOWN: {exc}",
                "Cannot establish whether this tree is current; refusing to report it as such.",
            ),
        )
    return evaluate(
        head=head,
        tip=tip,
        merge_base=merge_base(repo, head, tip),
        mode=mode,
        label=label,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Assert a checkout is current with its tracked branch before terraform "
        "plans or applies from it (bug eebc-6aa1-e45b-4325).",
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repository path")
    parser.add_argument("--remote", default=DEFAULT_REMOTE, help="tracked remote")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="tracked branch")
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=MODE_TIP,
        help="tip: HEAD must BE the tracked tip (the drift sweep). "
        "ancestor: HEAD must CONTAIN it (a PR merge commit, or a feature branch).",
    )
    parser.add_argument(
        "--no-fetch",
        dest="fetch",
        action="store_false",
        help="do not contact the remote; compare against the local remote-tracking ref, "
        "which is only as fresh as the last fetch",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    verdict = check(
        args.repo,
        remote=args.remote,
        branch=args.branch,
        mode=args.mode,
        fetch=args.fetch,
    )
    stream = sys.stdout if verdict.is_current else sys.stderr
    for line in verdict.lines:
        print(line, file=stream)
    return verdict.code


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
