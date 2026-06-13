"""Auto-init + freshness middleware for the argparse CLI.

This is the in-process port of the bash dispatcher's ``_ensure_initialized``
(``_engine/rebar`` lines ~167-235). The dispatcher ran it before every command
arm; the argparse CLI runs it for the in-process (category-A) arms with the same
per-command policy:

* ``init_only=True`` (read arms: show/list/deps/ready/search/next-batch/list-epics)
  — auto-init only; the read path owns its own throttled reconverge
  (``rebar._engine_support.reads.ensure_fresh``), so the middleware must NOT
  reconverge too (that would double-sync).
* ``init_only=False`` (write/lifecycle arms) — auto-init **and** the same
  marker-throttled, fetch-free reconverge the bash write path did. We reuse
  ``reads.ensure_fresh`` for the reconverge so there is ONE sync implementation
  and ONE ``/tmp/.ticket-sync-<md5>`` throttle marker shared with the read path.

Category-B arms still subprocess the bash dispatcher (which runs its own
``_ensure_initialized``), so the middleware is intentionally NOT applied to them.

When ``TICKETS_TRACKER_DIR`` is injected (tests / embedding) the caller owns the
tracker — the middleware returns immediately, exactly as the dispatcher did.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


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
    root = os.environ.get("PROJECT_ROOT") or os.environ.get("REBAR_ROOT")
    if not root:
        root = _git_toplevel()
    if not root:
        sys.stderr.write(
            "Error: not inside a git repository (set REBAR_ROOT or run inside the repo)\n"
        )
        raise SystemExit(1)
    return root


def _auto_init(repo_root: str) -> None:
    """Bootstrap the tracker when absent (the dispatcher's ``ticket-init.sh --silent``).

    Transitional: until Tier E ports ``init`` in-process, auto-init subprocesses the
    bundled dispatcher's ``init --silent`` arm — the same code the dispatcher ran.
    """
    from rebar._engine import dispatcher, engine_env

    r = subprocess.run(
        ["bash", str(dispatcher()), "init", "--silent"],
        stdout=subprocess.DEVNULL,
        env=engine_env(repo_root),
        cwd=repo_root,
    )
    if r.returncode != 0:
        sys.stderr.write(
            "Error: ticket system initialization failed. Run 'rebar init' manually.\n"
        )
        raise SystemExit(1)


def ensure_initialized(*, init_only: bool) -> None:
    """Port of the dispatcher's ``_ensure_initialized`` for in-process arms."""
    # Explicit tracker injected → the caller manages init/freshness (do not
    # auto-init the cwd repo's tracker). Matches the dispatcher's first guard.
    if os.environ.get("TICKETS_TRACKER_DIR"):
        return

    repo_root = _resolve_repo_root()
    if not (Path(repo_root) / ".tickets-tracker").is_dir():
        _auto_init(repo_root)

    if init_only:
        return

    # Full mode: marker-throttled, fetch-free reconverge — the SAME implementation
    # and the SAME throttle marker the read path uses (reads.ensure_fresh honors
    # REBAR_NO_SYNC / _TICKET_TEST_NO_SYNC, mirroring the dispatcher's sync guard).
    from rebar._engine_support import reads

    reads.ensure_fresh(reads.tracker_dir(repo_root))
