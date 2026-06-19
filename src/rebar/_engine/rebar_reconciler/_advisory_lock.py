"""Advisory lock primitives for the reconciler — tickets-branch pass-lock + phase-gate.

Provides:
  ReconcileLockError  — raised on fail-CLOSED conditions (missing branch, unknown errors)
  check_pass_lock     — returns True/False; raises ReconcileLockError on fail-CLOSED
  acquire_pass_lock   — write lock file to tickets branch via rebase_retry
  release_pass_lock   — delete lock file from tickets branch via rebase_retry
                        (ownership check: mismatch → warn + no-op, no exception)
  check_phase_gate    — returns True if advancement is blocked by gate file

All tickets-branch writes MUST go through rebase_retry from _concurrency.py
(plan-review F3: do not invent new write paths).
"""

from __future__ import annotations

import importlib.util
import logging
import random
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy import of _concurrency to respect the importlib.util loading convention
# ---------------------------------------------------------------------------

_CONCURRENCY_PATH = Path(__file__).parent / "_concurrency.py"


def _load_concurrency():
    """Load _concurrency module, caching in sys.modules."""
    key = "rebar_reconciler__concurrency_advisory"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, _CONCURRENCY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _rebase_retry(repo_root: Path, write_fn, **kwargs):
    """Thin wrapper so tests can monkeypatch advisory_lock._rebase_retry."""
    concurrency = _load_concurrency()
    return concurrency.rebase_retry(repo_root, write_fn, **kwargs)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOCK_FILE = ".reconciler-pass-lock"
_GATE_FILE = ".reconciler-phase-gate"

# ---------------------------------------------------------------------------
# Retry/jitter band-aid for lock contention (bug b859-8fa1).
#
# acquire_pass_lock wraps _rebase_retry with an outer retry loop so that
# concurrent harness writes (autosaves, ticket comments) do not exhaust the
# inner 3-attempt drift budget on the first try. Production CI sees no
# harness contention, so the default of 5 outer attempts keeps the failure
# envelope tight while unblocking dev probes.
# ---------------------------------------------------------------------------

# The env names (canonical REBAR_RECONCILER_LOCK_MAX_RETRIES, deprecated alias
# REBAR_RECONCILER_LOCK_RETRY_BUDGET) are now owned by the typed config layer
# (rebar.config); _resolve_retry_budget reads the resolved value via load_config.
_LOCK_RETRY_BUDGET_DEFAULT = 5
_BACKOFF_BASE_SECONDS = 0.2  # 200ms
_BACKOFF_FACTOR = 2.0
_BACKOFF_CAP_SECONDS = 5.0
_BACKOFF_JITTER_FRACTION = 0.3  # ±30%

# ---------------------------------------------------------------------------
# Compare-and-swap (CAS) race retry budget for the tickets-ref advance
# (bug 1f47-9337-3db0-4f3c).
#
# The read-tip -> build-commit -> `git update-ref refs/heads/tickets <new>
# <old>` sequence is a single-shot CAS. When a concurrent tickets-branch writer
# (ticket-CLI event commit, per-pass bindings commit, agent comment) advances
# the ref between the old-sha read and the CAS, update-ref exits 128 (old-sha
# mismatch). That exit-128 is a *benign* race, not a fault: we re-read the new
# tip, rebuild the commit on it, and retry the CAS. The budget is bounded so a
# pathological writer cannot induce an infinite loop — exhaustion surfaces as a
# CalledProcessError to rebase_retry (abort_due_to_error), preserving
# fail-CLOSED behaviour. This mirrors rebase_retry's own drift idiom (re-pin
# the new HEAD and retry) but operates one level lower, at the ref-advance CAS,
# where rebase_retry's before/after snapshot cannot see the race.
# ---------------------------------------------------------------------------

_CAS_RETRY_BUDGET = 8
_CAS_BACKOFF_BASE_SECONDS = 0.05  # 50ms
_CAS_BACKOFF_CAP_SECONDS = 1.0
_CAS_BACKOFF_JITTER_FRACTION = 0.3  # ±30%


def _resolve_retry_budget() -> int:
    """Return the outer retry budget (>=1), resolved through the typed config.

    Reads ``[tool.rebar.reconciler].lock_max_retries`` (default 5), overridden by env
    ``REBAR_RECONCILER_LOCK_MAX_RETRIES`` (deprecated alias
    ``REBAR_RECONCILER_LOCK_RETRY_BUDGET``), then by
    ``rebar -c reconciler.lock_max_retries=…``. 0 disables the outer retry
    (equivalent to a single attempt, today's behaviour); an unreadable/invalid config
    falls back to the default rather than failing the pass.
    """
    from rebar.config import ConfigError, load_config

    try:
        value = load_config().reconciler.lock_max_retries
    except ConfigError:
        return _LOCK_RETRY_BUDGET_DEFAULT
    # Treat 0 as "disable outer retry" — equivalent to 1 attempt (today's behaviour).
    return max(1, value)


def _compute_backoff_seconds(retry_index: int) -> float:
    """Return jittered backoff for the *retry_index*-th retry (0-indexed).

    Schedule: base * factor**retry_index, capped at cap, then multiplied by
    a uniform factor in [1 - jitter, 1 + jitter]. Stdlib only (random.uniform).
    """
    base = min(
        _BACKOFF_BASE_SECONDS * (_BACKOFF_FACTOR**retry_index),
        _BACKOFF_CAP_SECONDS,
    )
    jitter = random.uniform(1.0 - _BACKOFF_JITTER_FRACTION, 1.0 + _BACKOFF_JITTER_FRACTION)
    return base * jitter


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ReconcileLockError(RuntimeError):
    """Raised on fail-CLOSED conditions in the advisory lock subsystem.

    Fail-CLOSED means: when we cannot determine lock state confidently (e.g.
    missing tickets branch, unrecognised git error), we block the orchestrator
    rather than silently disabling concurrency protection.
    """


# ---------------------------------------------------------------------------
# git show helper with stderr discrimination (AC amendment G4)
# ---------------------------------------------------------------------------


def _git_show_tickets_file(repo_root: Path, filename: str) -> str | None:
    """Read *filename* from the tickets branch using ``git show``.

    Returns:
        str  — file contents if the file exists on the tickets branch.
        None — if the file is absent on tickets branch (normal, not an error).

    Raises:
        ReconcileLockError — if the tickets branch itself is missing, or if an
            unrecognised non-zero exit occurs (fail-CLOSED discipline).

    Stderr discrimination (G4):
        - exit 0                       → return stdout (file present)
        - exit != 0, 'unknown revision' in stderr  → tickets branch missing → raise
        - exit != 0, 'does not exist in' in stderr → file absent on branch  → None
        - exit != 0, anything else                 → unrecognised error     → raise
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"tickets:{filename}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout

    stderr = result.stderr or ""

    if "unknown revision" in stderr or "unknown ref" in stderr:
        raise ReconcileLockError(
            f"tickets branch not found in {repo_root}: {stderr.strip()!r} — "
            "cannot determine lock state (fail-CLOSED)"
        )

    if "does not exist in" in stderr or "exists on disk, but not in" in stderr:
        # File is absent on the tickets branch — normal no-lock state
        return None

    # Unrecognised error — fail-CLOSED
    raise ReconcileLockError(
        f"git show tickets:{filename} returned exit {result.returncode} with "
        f"unrecognised stderr: {stderr.strip()!r} (fail-CLOSED)"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_pass_lock(repo_root: Path) -> bool:
    """Return True if .reconciler-pass-lock is present on the tickets branch.

    Returns False when the lock file is absent (normal — no active lock).

    Raises:
        ReconcileLockError — if the tickets branch is missing or an unrecognised
            git error occurs (fail-CLOSED).
    """
    contents = _git_show_tickets_file(repo_root, _LOCK_FILE)
    return contents is not None


def acquire_pass_lock(pass_id: str, repo_root: Path) -> None:
    """Write .reconciler-pass-lock to the tickets branch via rebase_retry.

    The lock file contains *pass_id* + timestamp_ns on separate lines so that
    release_pass_lock can verify ownership before deletion.

    Uses rebase_retry from _concurrency.py (plan-review F3 alignment: existing
    serialization-safe write path; no new raw git commit path introduced).

    Raises:
        ReconcileLockError — if rebase_retry fails (drift exhaustion or error).
    """
    timestamp_ns = time.time_ns()
    lock_contents = f"{pass_id}\n{timestamp_ns}\n"

    def _write():
        _write_file_to_tickets_branch(
            repo_root, _LOCK_FILE, lock_contents, f"acquire lock pass_id={pass_id}"
        )

    # Bug b859-8fa1 band-aid: wrap _rebase_retry in an outer retry loop with
    # exponential backoff + jitter so that concurrent harness writes (autosaves,
    # ticket comments) do not blow out the inner drift budget on first attempt.
    # Non-drift errors (abort_due_to_error) still fail fast — only the
    # reject_and_reschedule outcome triggers the outer retry.
    budget = _resolve_retry_budget()
    last_result = None
    for attempt in range(1, budget + 1):
        result = _rebase_retry(repo_root, _write)
        if result.ok:
            return
        last_result = result
        kind = result.event.kind if result.event else "unknown"
        # Fail-fast for non-drift errors; only retry reject_and_reschedule.
        if kind != "reject_and_reschedule":
            break
        if attempt >= budget:
            break
        backoff = _compute_backoff_seconds(attempt - 1)
        logger.info(
            "acquire_pass_lock: drift retry %d/%d for pass_id=%r — "
            "sleeping %.3fs before next attempt (last: %s)",
            attempt,
            budget,
            pass_id,
            backoff,
            result.event.message if result.event else "",
        )
        time.sleep(backoff)

    # Exhausted budget or hit non-drift error.
    event = last_result.event if last_result else None
    raise ReconcileLockError(
        f"acquire_pass_lock failed for pass_id={pass_id!r}: "
        f"{event.kind if event else 'unknown'}: "
        f"{event.message if event else ''}"
    )


def release_pass_lock(pass_id: str, repo_root: Path) -> None:
    """Delete .reconciler-pass-lock from the tickets branch via rebase_retry.

    Ownership check (G5): reads the existing lock contents and verifies the
    stored pass_id matches *pass_id* before deletion. On mismatch, logs a
    warning and returns without raising (defensive — never disrupt the caller,
    never unlock another process's lock).

    Idempotent: if the lock file is absent, returns silently.

    Raises:
        ReconcileLockError — if the tickets branch is missing.
    """
    # Ownership check before attempting deletion
    try:
        contents = _git_show_tickets_file(repo_root, _LOCK_FILE)
    except ReconcileLockError:
        raise

    if contents is None:
        # Already absent — idempotent success
        return

    # Parse pass_id from first line
    stored_pass_id = contents.splitlines()[0].strip() if contents else ""
    if stored_pass_id != pass_id:
        logger.warning(
            "release_pass_lock: pass_id mismatch — stored owner %r does not match "
            "caller %r; leaving lock in place (defensive owner-check)",
            stored_pass_id,
            pass_id,
        )
        return

    def _delete():
        _delete_file_from_tickets_branch(repo_root, _LOCK_FILE, f"release lock pass_id={pass_id}")

    result = _rebase_retry(repo_root, _delete)
    if not result.ok:
        raise ReconcileLockError(
            f"release_pass_lock failed for pass_id={pass_id!r}: "
            f"{result.event.kind if result.event else 'unknown'}: "
            f"{result.event.message if result.event else ''}"
        )


def check_phase_gate(target_mode, repo_root: Path) -> bool:
    """Return True if *target_mode* is blocked by the phase gate on tickets branch.

    The gate file (.reconciler-phase-gate) contains the MODE name at or below
    which advancement is permitted. If *target_mode* has a strictly higher rank
    than the gated mode, the gate blocks advancement.

    Gate semantics (operator-based, via Mode.__lt__ + @functools.total_ordering):
        blocked iff target_mode > gated_mode

    Returns False (not blocked) when:
        - The gate file is absent on the tickets branch.
        - target_mode <= gated_mode (within permitted range).

    Raises:
        ReconcileLockError — if the tickets branch is missing or an unrecognised
            git error occurs (fail-CLOSED, matching :func:`check_pass_lock`).

    Example:
        Gate file contains 'bootstrap-strict' (rank 1).
        BOOTSTRAP_THROTTLE (rank 2) → blocked (2 > 1) → True.
        BOOTSTRAP_STRICT (rank 1)   → not blocked (1 == 1) → False.

    The Mode enum is imported from mode.py at call time so this module does
    not hard-code ordering.
    """
    # Fail-CLOSED on missing tickets branch (alignment with check_pass_lock):
    # if we cannot determine the gate state, refuse to proceed rather than
    # silently disabling phase-gate protection. Bug from coderabbit review —
    # previously this path returned False (fail-open) while check_pass_lock
    # raised; the asymmetry let phase-gate violations slip past when the
    # tickets branch was absent.
    contents = _git_show_tickets_file(repo_root, _GATE_FILE)

    if contents is None:
        # Gate file absent — no block
        return False

    gated_mode_str = contents.strip()
    if not gated_mode_str:
        return False

    # Load mode.py under the SAME canonical dotted key that __main__.py uses
    # so tests (which pre-seed sys.modules under that key) and production code
    # share a single Mode class object. Loading under a private key produced
    # two distinct Mode class identities — isinstance checks across module
    # boundaries silently mis-routed.
    mode_key = "rebar_reconciler.mode"
    if mode_key in sys.modules:
        mode_mod = sys.modules[mode_key]
    else:
        mode_path = Path(__file__).parent / "mode.py"
        spec = importlib.util.spec_from_file_location(mode_key, mode_path)
        assert spec is not None and spec.loader is not None
        mode_mod = importlib.util.module_from_spec(spec)
        sys.modules[mode_key] = mode_mod
        spec.loader.exec_module(mode_mod)  # type: ignore[union-attr]

    try:
        gated_mode = mode_mod.Mode.from_str(gated_mode_str)
    except ValueError:
        logger.warning(
            "check_phase_gate: unrecognised mode %r in gate file; treating as no gate",
            gated_mode_str,
        )
        return False

    # Natural < / > operators provided by @functools.total_ordering on Mode.
    return target_mode > gated_mode


# ---------------------------------------------------------------------------
# Low-level tickets-branch file write/delete helpers
# ---------------------------------------------------------------------------


def _is_cas_mismatch(exc: subprocess.CalledProcessError) -> bool:
    """Return True iff *exc* is an ``update-ref`` compare-and-swap old-sha mismatch.

    ``git update-ref refs/heads/tickets <new> <old>`` exits 128 when the ref no
    longer points at <old> (a concurrent writer advanced it). We discriminate on
    BOTH the command shape (an ``update-ref`` invocation) and the exit code so an
    unrelated exit-128 from some other git command is not misclassified as a
    retryable race.
    """
    args = exc.cmd or []
    is_update_ref = "update-ref" in args and "refs/heads/tickets" in args
    return is_update_ref and exc.returncode == 128


def _cas_backoff_seconds(retry_index: int) -> float:
    """Jittered backoff for the *retry_index*-th CAS retry (0-indexed).

    Short backoff (50ms base) capped at 1s — a CAS race resolves in the time it
    takes a concurrent writer to land its commit, far quicker than the
    coarse-grained outer lock-acquire backoff.
    """
    base = min(
        _CAS_BACKOFF_BASE_SECONDS * (_BACKOFF_FACTOR**retry_index),
        _CAS_BACKOFF_CAP_SECONDS,
    )
    jitter = random.uniform(1.0 - _CAS_BACKOFF_JITTER_FRACTION, 1.0 + _CAS_BACKOFF_JITTER_FRACTION)
    return base * jitter


def _cas_advance_with_retry(repo_root: Path, mutate_and_advance) -> None:
    """Run *mutate_and_advance* with bounded retry on a tickets-ref CAS mismatch.

    *mutate_and_advance* is a zero-arg callable that performs the full
    read-tip -> build-commit-in-detached-worktree -> ``update-ref`` CAS
    sequence. Because the commit is built on top of the tip it read, a CAS
    mismatch means the commit was built on a now-stale tip; the only correct
    recovery is to re-run the WHOLE sequence so the commit is rebuilt on the new
    tip. We therefore retry the callable as a unit.

    Retries ONLY on a CAS old-sha mismatch (:func:`_is_cas_mismatch`). Any other
    ``CalledProcessError`` (or other exception) propagates immediately
    (fail-CLOSED — genuine faults are not masked). Exhausting
    ``_CAS_RETRY_BUDGET`` re-raises the last CAS-mismatch error so the caller's
    ``rebase_retry`` wrapper records it as ``abort_due_to_error`` rather than
    looping forever.
    """
    for attempt in range(1, _CAS_RETRY_BUDGET + 1):
        try:
            mutate_and_advance()
            return
        except subprocess.CalledProcessError as exc:
            if not _is_cas_mismatch(exc):
                # Not a CAS race — propagate (fail-CLOSED).
                raise
            if attempt >= _CAS_RETRY_BUDGET:
                logger.warning(
                    "tickets-ref CAS retry budget (%d) exhausted; concurrent "
                    "writers kept advancing the ref — surfacing as error",
                    _CAS_RETRY_BUDGET,
                )
                raise
            backoff = _cas_backoff_seconds(attempt - 1)
            logger.info(
                "tickets-ref CAS mismatch (concurrent writer advanced ref); "
                "rebuilding on new tip — retry %d/%d after %.3fs",
                attempt,
                _CAS_RETRY_BUDGET,
                backoff,
            )
            time.sleep(backoff)


def _commit_in_detached_tickets_worktree(repo_root: Path, commit_message: str, mutate_fn) -> None:
    """Run *mutate_fn* against a fresh detached worktree of the tickets branch, then
    commit + advance the branch ref via compare-and-swap (with retry).

    The single CAS frame shared by :func:`_write_file_to_tickets_branch` and
    :func:`_delete_file_from_tickets_branch` (was duplicated near-verbatim). ``mutate_fn``
    takes the worktree dir (``Path``) and *stages* its change (write+add, or ``git rm``);
    whether a commit happens is decided UNIFORMLY by the staged-diff guard — so an
    idempotent retry (nothing to write / file already gone) stages nothing, makes no
    commit, and leaves HEAD unchanged for rebase_retry's drift guard. (The delete path's
    former ``if file_path.exists()`` commit predicate is normalized to this staged-diff
    guard: a ``git rm`` of an existing file stages a deletion; a no-op stages nothing —
    same outcome.) The ``--detach`` worktree keeps the tickets branch pointer unlocked so
    concurrent callers coexist; the ref is advanced atomically via ``update-ref`` CAS.
    """
    import shutil as _shutil
    import tempfile as _tempfile

    def _mutate_and_advance() -> None:
        # Snapshot the tickets HEAD *before* the commit so the CAS old-sha matches
        # rebase_retry's before-snapshot. Read inside the retried unit so a CAS race
        # re-reads the new tip and the change is rebuilt on it (bug 1f47-9337-3db0-4f3c).
        old_sha_result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "tickets"],
            capture_output=True,
            text=True,
            check=False,
        )
        if old_sha_result.returncode != 0:
            raise subprocess.CalledProcessError(
                old_sha_result.returncode,
                old_sha_result.args,
                old_sha_result.stdout,
                old_sha_result.stderr,
            )
        old_sha = old_sha_result.stdout.strip()

        # Stale-worktree pre-flight: prune dangling registrations from a crashed prior
        # run, then defensively remove the target path (mktemp prefix reuse edge case).
        worktree_parent = Path(_tempfile.mkdtemp(prefix="advisory-lock-wt-parent-"))
        worktree_dir = worktree_parent / "wt"
        subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "prune"],
            capture_output=True,
            check=False,
        )
        if worktree_dir.exists():
            subprocess.run(
                ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(worktree_dir)],
                capture_output=True,
                check=False,
            )
            import shutil as _shutil_preflight

            _shutil_preflight.rmtree(worktree_dir, ignore_errors=True)
        try:
            # --detach avoids "fatal: 'tickets' is already used by worktree at ..." when a
            # sibling worktree (e.g. .tickets-tracker) has tickets checked out.
            _git_run(repo_root, ["worktree", "add", "--detach", str(worktree_dir), "tickets"])
            mutate_fn(worktree_dir)
            # Commit ONLY if there are staged changes (idempotent guard for retries and
            # for a no-op mutate — see the docstring's predicate-normalization note).
            status = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                capture_output=True,
                check=False,
                cwd=str(worktree_dir),
            )
            if status.returncode != 0:
                _git_run_in(worktree_dir, ["commit", "-m", commit_message])
                new_sha_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=True,
                    cwd=str(worktree_dir),
                )
                new_sha = new_sha_result.stdout.strip()
                # compare-and-swap: only advance if tickets still points to old_sha. A
                # concurrent writer advancing the ref here makes this exit 128;
                # _cas_advance_with_retry re-runs this whole closure on the new tip.
                _git_run(repo_root, ["update-ref", "refs/heads/tickets", new_sha, old_sha])
        finally:
            try:
                _git_run(repo_root, ["worktree", "remove", "--force", str(worktree_dir)])
            except subprocess.CalledProcessError:
                # Best-effort cleanup: if the worktree remove fails (e.g. already gone),
                # ignore it — rmtree below still cleans up the temp dir.
                pass
            _shutil.rmtree(worktree_parent, ignore_errors=True)

    _cas_advance_with_retry(repo_root, _mutate_and_advance)


def _write_file_to_tickets_branch(
    repo_root: Path, filename: str, contents: str, commit_message: str
) -> None:
    """Write *contents* to *filename* on the tickets orphan branch.

    Uses a temporary git worktree for the tickets branch so the main working
    tree branch pointer never changes.  This keeps rebase_retry's drift guard
    honest: only commits by *other* passes advance the tickets HEAD between our
    before/after snapshots.

    If the file already contains *contents* (idempotent retry path), the write
    is skipped and no commit is made — leaving HEAD unchanged so rebase_retry
    can detect no-drift and return ok=True on the retry pass.

    Uses ``--detach`` when creating the temporary worktree so that the tickets
    branch pointer is never exclusively locked to this worktree.  This allows
    concurrent callers (e.g. a CI pre-flight step that has mounted tickets as
    ``.tickets-tracker``) to coexist without a ``fatal: 'tickets' is already
    used by worktree at ...`` exit-128 error.  After committing in the detached
    worktree the tickets branch ref is advanced atomically via
    ``git update-ref`` compare-and-swap.
    """
    def _mutate(worktree_dir: Path) -> None:
        (worktree_dir / filename).write_text(contents)
        _git_run_in(worktree_dir, ["add", filename])

    _commit_in_detached_tickets_worktree(repo_root, commit_message, _mutate)


def _delete_file_from_tickets_branch(repo_root: Path, filename: str, commit_message: str) -> None:
    """Delete *filename* from the tickets orphan branch.

    Uses a temporary git worktree so the main branch pointer is unchanged.
    Idempotent: if the file is absent, does nothing.

    Uses ``--detach`` when creating the temporary worktree so that the tickets
    branch pointer is never exclusively locked to this worktree.  After
    committing the deletion in the detached worktree the tickets branch ref is
    advanced atomically via ``git update-ref`` compare-and-swap.
    """
    def _mutate(worktree_dir: Path) -> None:
        # Idempotent: ``git rm`` only an existing file (a no-op stages nothing, so the
        # shared staged-diff guard then makes no commit — same as the former
        # ``if file_path.exists()`` commit predicate).
        if (worktree_dir / filename).exists():
            _git_run_in(worktree_dir, ["rm", "-f", filename])

    _commit_in_detached_tickets_worktree(repo_root, commit_message, _mutate)


def _current_branch(repo_root: Path) -> str:
    """Return the current branch name, or empty string on detached HEAD / failure."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def _git_run(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess:
    """Run a git command in repo_root; raise CalledProcessError on non-zero exit."""
    return subprocess.run(
        ["git", "-C", str(repo_root)] + args,
        capture_output=True,
        text=True,
        check=True,
    )


def _git_run_in(directory: Path, args: list[str]) -> subprocess.CompletedProcess:
    """Run a git command with CWD set to *directory* (for temporary worktrees)."""
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        check=True,
        cwd=str(directory),
    )
