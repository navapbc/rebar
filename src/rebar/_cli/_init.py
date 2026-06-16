"""Auto-init + freshness middleware for the in-process CLI.

The CLI runs this before each in-process command arm, with a per-command policy:

* ``init_only=True`` (read arms: show/list/deps/ready/search/next-batch/list-epics)
  — auto-init only; the read path owns its own throttled reconverge
  (``rebar._engine_support.reads.ensure_fresh``), so the middleware must NOT
  reconverge too (that would double-sync).
* ``init_only=False`` (write/lifecycle arms) — auto-init **and** the same
  marker-throttled, fetch-free reconverge the write path needs. It reuses
  ``reads.ensure_fresh`` for the reconverge so there is ONE sync implementation
  and ONE ``/tmp/.ticket-sync-<md5>`` throttle marker shared with the read path.

When ``TICKETS_TRACKER_DIR`` is injected (tests / embedding) the caller owns the
tracker — the middleware returns immediately.
"""

from __future__ import annotations

import os
import subprocess
import sys


def _git_toplevel() -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None
    out = r.stdout.strip()
    return out if (r.returncode == 0 and out) else None


def _resolve_repo_root() -> str:
    """Repo root with the dispatcher's precedence: PROJECT_ROOT, REBAR_ROOT, git.

    Exits 1 with the dispatcher's exact message when none resolves.
    """
    # Same precedence as config.repo_root (REBAR_ROOT > PROJECT_ROOT > git) so the
    # gate inspects the SAME repo the commands operate on and init writes to.
    root = os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT")
    if not root:
        root = _git_toplevel()
    if not root:
        sys.stderr.write(
            "Error: not inside a git repository (set REBAR_ROOT or run inside the repo)\n"
        )
        raise SystemExit(1)
    return root


def _is_interactive() -> bool:
    """True only when both stdin and stderr are TTYs — i.e. a human can answer a
    prompt. CI/pipes/tests are non-interactive."""
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _create_tracker(repo_root: str) -> None:
    """Materialize the missing tracker, distinguishing the two init concepts.

    The store at ``repo_root`` does not exist yet, but "make it exist" means one of
    two very different things, and only one of them changes the underlying repo:

    * **Symlink to an existing store** — when the host repo is a linked git
      worktree whose MAIN repo is already initialized, ``init_core`` just creates a
      ``.tickets-tracker`` symlink to the main repo's store. That adds a local link
      to an *existing* system and leaves the underlying repo's state untouched, so
      we create it AUTOMATICALLY — no prompt, even non-interactively.
    * **First-time init** — when there is no store to link to, materializing one
      mutates the host repo (orphan ``tickets`` branch + linked worktree +
      ``.git/info/exclude`` edits). That requires consent (see
      :func:`_confirm_and_init`).
    """
    from rebar._commands import init as _init_cmd

    if _init_cmd.pending_init_is_symlink(repo_root):
        if _init_cmd.init_core(repo_root, silent=False) != 0:
            sys.stderr.write(
                "Error: could not link this worktree to the main repo's ticket store. "
                "Run 'rebar init' manually.\n"
            )
            raise SystemExit(1)
        return
    _confirm_and_init(repo_root)


def _confirm_and_init(repo_root: str) -> None:
    """First-time-init consent gate: a NEW ticket store is never
    created without consent.

    Reached only when there is no existing store to symlink to (see
    :func:`_create_tracker`), so creating one mutates the host repo. Interactive
    (TTY): prompt ``[Y/n]`` (default Yes); a No aborts. Non-interactive
    (CI/pipe/library/MCP-shaped): error — no silent creation. The explicit
    ``rebar init`` / :func:`rebar.init_repo` paths bypass this gate entirely. init
    runs in-process via :func:`rebar._commands.init.init_core`.

    Prior-art rationale: git-attached trackers split into silent-implicit creation
    (git-bug/git-appraise, ref-only storage — invisible, cheap to auto-create) and
    explicit-init (git-issue/bugs-everywhere/Fossil). rebar joins the explicit camp
    and goes further (consent or explicit, never silent in automation) because its
    init mutates the WORKING TREE — an orphan ``tickets`` branch, a linked worktree,
    and ``.git/info/exclude`` edits — a far heavier footprint than git-bug's refs,
    so silently materializing it on a stray read would surprise the user.
    """
    if not _is_interactive():
        sys.stderr.write(
            "Error: ticket system not initialized. Run 'rebar init' first "
            "(auto-init requires an interactive terminal).\n"
        )
        raise SystemExit(1)

    sys.stderr.write("Ticket system not initialized in this repo. Initialize now? [Y/n] ")
    sys.stderr.flush()
    try:
        answer = input().strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("", "y", "yes"):
        sys.stderr.write(
            "Aborted: ticket system not initialized. Run 'rebar init' to initialize.\n"
        )
        raise SystemExit(1)

    from rebar._commands import init as _init_cmd

    if _init_cmd.init_core(repo_root, silent=False) != 0:
        sys.stderr.write("Error: ticket system initialization failed. Run 'rebar init' manually.\n")
        raise SystemExit(1)


def ensure_initialized(*, init_only: bool) -> None:
    """Auto-init + freshness gate for in-process CLI arms.

    This never creates a NEW store without an interactive confirmation (TTY) — non-interactive
    callers must run ``rebar init`` / :func:`rebar.init_repo` explicitly first.
    Creating a worktree's symlink to an ALREADY-initialized store is the one
    exception: it doesn't change the underlying repo, so it happens automatically
    (see :func:`_create_tracker`).
    """
    # Explicit tracker injected → the caller manages init/freshness (do not
    # auto-init the cwd repo's tracker). Matches the dispatcher's first guard.
    if os.environ.get("TICKETS_TRACKER_DIR"):
        return

    repo_root = _resolve_repo_root()
    # Check existence at the SAME location init writes / commands read
    # (config.tracker_dir), not a hard-coded repo_root/.tickets-tracker — otherwise a
    # REBAR_ROOT/PROJECT_ROOT that differs from the git toplevel would re-prompt
    # forever (the check never sees the tracker init actually created).
    from rebar import config

    if not config.tracker_dir(repo_root).is_dir():
        _create_tracker(repo_root)

    if init_only:
        return

    # Full mode: marker-throttled, fetch-free reconverge — the SAME implementation
    # and the SAME throttle marker the read path uses (reads.ensure_fresh honors the
    # sync.pull policy: REBAR_SYNC_PULL=off, deprecated alias REBAR_NO_SYNC).
    from rebar._engine_support import reads

    reads.ensure_fresh(reads.tracker_dir(repo_root))
