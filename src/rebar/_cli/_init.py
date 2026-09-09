"""Apply CLI auto-initialization and freshness policy.

Read arms initialize only because their read path owns reconvergence. Write and
lifecycle arms also reuse the read path's throttled, fetch-free reconvergence.
An injected tracker override leaves both responsibilities to the caller.
"""

from __future__ import annotations

import logging
import sys

_log = logging.getLogger(__name__)


def _resolve_repo_root() -> str:
    """Repo root with precedence REBAR_ROOT, then git.

    Exits 1 with the canonical message when none resolves.
    """
    # Same precedence as config.repo_root (REBAR_ROOT > git) so the
    # gate inspects the SAME repo the commands operate on and init writes to.
    from rebar import config

    root = config.repo_root_or_none()
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
    """Materialize a missing tracker through the appropriate initialization path.

    Linking a worktree to an existing store is local and automatic. Creating the
    first store changes repository state and therefore delegates to the consent gate.
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
    # An existing local or remote tickets branch is shared state, so attach automatically.
    # Only true first-store creation requires consent.
    if _init_cmd.pending_init_attaches_to_existing(repo_root):
        if _init_cmd.init_core(repo_root, silent=False) != 0:
            sys.stderr.write(
                "Error: could not attach to the existing ticket store. Run 'rebar init' manually.\n"
            )
            raise SystemExit(1)
        return
    if _init_cmd.pending_init_remote_unreachable(repo_root):
        from rebar import config as _config
        from rebar._commands import _init_probe

        remote = _config.tickets_remote(repo_root)
        branch = _config.tickets_branch(repo_root)
        sys.stderr.write(
            f"Error: could not determine whether {remote}/{branch} exists within "
            f"{_init_probe.REMOTE_PROBE_TIMEOUT}s; auto-init is refusing to create a new store. "
            "Retry after connectivity returns, or run 'rebar init --force-new-store' explicitly.\n"
        )
        raise SystemExit(1)
    _confirm_and_init(repo_root)


def _confirm_and_init(repo_root: str) -> None:
    """Require consent before creating the repository's first ticket store.

    Interactive callers may accept the ``[Y/n]`` prompt. Non-interactive callers
    must run an explicit initialization API or command. This path creates the orphan
    branch, linked worktree, and exclude entry, so it never runs implicitly in
    automation.
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
    """Initialize a CLI arm and refresh it when ``init_only`` is false.

    First-store creation still needs interactive consent. Linking to an existing
    store is automatic.
    """
    # Explicit tracker injected → the caller manages init/freshness (do not
    # auto-init the cwd repo's tracker).
    from rebar import config

    if config.tracker_dir_override():
        return

    repo_root = _resolve_repo_root()
    # Check the configured tracker path, which initialization writes, to avoid
    # repeated prompts when ``REBAR_ROOT`` differs from Git's top level.
    from rebar import config

    if not config.tracker_dir(repo_root).is_dir():
        _create_tracker(repo_root)

    if init_only:
        return

    # Full mode: marker-throttled, fetch-free reconverge — the SAME implementation
    # and the SAME throttle marker the read path uses (reads.ensure_fresh honors the
    # sync.pull policy: REBAR_SYNC_PULL=off.
    from rebar._engine_support import reads

    reads.ensure_fresh(reads.tracker_dir(repo_root))


def ensure_store_mounted_best_effort() -> None:
    """Attach an existing store before any dispatched command that may need one.

    This best-effort gate covers intercepts that run before per-arm initialization.
    It may link a worktree or attach an existing tickets branch, but never creates
    the first store, reconverges, or raises. Strict per-arm initialization retains
    greenfield refusal and freshness ownership.
    """
    from rebar import config

    # Explicit tracker injected → embedder/test owns the tracker (same as
    # ensure_initialized); do not auto-mount the cwd repo's tracker.
    if config.tracker_dir_override():
        return

    try:
        # Resolve ``REBAR_ROOT`` before Git. Absence is harmless because storeless commands
        # may run outside a repository.
        root = config.repo_root_or_none()
        if not root:
            return
        if config.tracker_dir(root).is_dir():
            return  # already mounted
        from rebar._commands import init as _init_cmd

        # Only the auto-attachable cases — a genuine greenfield first-time init is
        # left to the strict per-arm ensure_initialized, never forced here.
        if _init_cmd.pending_init_is_symlink(root) or _init_cmd.pending_init_attaches_to_existing(
            root
        ):
            _init_cmd.init_core(root, silent=True)  # nonzero → best-effort no-op
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — best-effort: never propagate to CLI
        _log.debug("best-effort store mount skipped: %s", exc)
