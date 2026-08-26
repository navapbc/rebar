"""Executable behaviour tests for the MCP container entrypoint's store provisioning.

These RUN `infra/scripts/mcp-entrypoint.sh --provision-only` in a temp directory with a
stubbed `git` on PATH. Nothing here greps the script's source: each test drives a real
store shape (poisoned / healthy / unsafe / contended) and asserts on what the script
actually DID — which `git` subcommands it invoked and what survived in the directory.

Why that matters: the previous tests could only assert that certain command tokens appeared
in `Dockerfile.mcp` in a certain order. Those pass whether or not the script works, and they
break on any refactor that preserves behaviour — a change detector on both counts. The
entrypoint was extracted into a real file precisely so this suite could exist.

Every "nothing happened" assertion here is paired with a LIVENESS ANCHOR (the script's
terminal `provisioning finished` line and the ensure-helper invocation), because an
absence-only assertion passes just as happily when the script died on line one.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_ENTRYPOINT = _REPO / "infra" / "scripts" / "mcp-entrypoint.sh"

_TICKETS_URL = "https://example.invalid/rebar.git"

# A stub `git` that records every invocation and answers the two questions the entrypoint
# asks: does HEAD resolve (controlled by the presence of a marker file), and can we clone
# (controlled by another marker). A successful clone materialises a store whose HEAD then
# resolves, exactly like the real thing.
_GIT_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$GIT_STUB_LOG"
# `git -C <dir> <cmd> ...` — skip the -C pair so the subcommand is $1.
if [ "$1" = "-C" ]; then shift 2; fi
case "$1" in
  config) exit 0 ;;
  rev-parse)
    [ -f "$GIT_STUB_STATE/head-resolves" ] && exit 0
    exit 128
    ;;
  clone)
    if [ -f "$GIT_STUB_STATE/clone-fails" ]; then
      echo "fatal: stub clone failure" >&2
      exit 128
    fi
    # Last argument is the destination.
    for dest in "$@"; do :; done
    mkdir -p "$dest/.git" "$dest/tickets"
    : > "$GIT_STUB_STATE/head-resolves"
    exit 0
    ;;
esac
exit 0
"""

# A stub for the shared ensure helper. Recording its invocation is the liveness anchor:
# it runs unconditionally at the END of provision_store, so its log proves the script did
# not die earlier.
_ENSURE_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$ENSURE_STUB_LOG"
[ -f "$GIT_STUB_STATE/ensure-fails" ] && exit 3
exit 0
"""


@dataclass
class Run:
    """One `--provision-only` execution and everything it left behind."""

    returncode: int
    stderr: str
    git_calls: list[str]
    ensure_calls: list[str]
    tracker: Path

    @property
    def cloned(self) -> bool:
        return any(call.startswith("clone ") for call in self.git_calls)

    @property
    def ran_to_completion(self) -> bool:
        """Liveness anchor: the ensure step ran AND the terminal log line was emitted."""
        return bool(self.ensure_calls) and "provisioning finished" in self.stderr


def _provision(
    tmp_path: Path,
    *,
    tracker_dir: str | None = None,
    pat: str | None = "stub-pat",
    head_resolves: bool = False,
    clone_fails: bool = False,
    ensure_fails: bool = False,
    env: dict[str, str] | None = None,
) -> Run:
    bin_dir = tmp_path / "bin"
    state = tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    tracker = tmp_path / "tracker"

    git = bin_dir / "git"
    git.write_text(_GIT_STUB)
    git.chmod(0o755)
    ensure = bin_dir / "ensure.sh"
    ensure.write_text(_ENSURE_STUB)
    ensure.chmod(0o755)

    git_log = tmp_path / "git.log"
    ensure_log = tmp_path / "ensure.log"
    for marker, on in (
        ("head-resolves", head_resolves),
        ("clone-fails", clone_fails),
        ("ensure-fails", ensure_fails),
    ):
        if on:
            (state / marker).touch()

    proc_env = subprocess_env(
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GIT_STUB_LOG": str(git_log),
            "GIT_STUB_STATE": str(state),
            "ENSURE_STUB_LOG": str(ensure_log),
            "MCP_ENSURE_SCRIPT": str(ensure),
            "MCP_TICKETS_URL": _TICKETS_URL,
            "REBAR_TRACKER_DIR": str(tracker) if tracker_dir is None else tracker_dir,
            # Keep the lock loop from ever sleeping through a test run.
            "MCP_RECLONE_LOCK_POLL": "1",
            "MCP_RECLONE_LOCK_WAIT": "2",
            **(env or {}),
        }
    )
    if pat is None:
        proc_env.pop("MCP_TICKETS_PAT", None)
    else:
        proc_env["MCP_TICKETS_PAT"] = pat

    result = subprocess.run(
        ["sh", str(_ENTRYPOINT), "--provision-only"],
        cwd=tmp_path,
        env=proc_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    def _lines(path: Path) -> list[str]:
        return path.read_text().splitlines() if path.exists() else []

    return Run(
        returncode=result.returncode,
        stderr=result.stderr,
        git_calls=[
            # Normalise away the `-C <dir>` prefix so assertions read on the subcommand.
            call.split(" ", 2)[2] if call.startswith("-C ") else call
            for call in _lines(git_log)
        ],
        ensure_calls=_lines(ensure_log),
        tracker=tracker,
    )


def _lock_path(tracker: Path) -> Path:
    """The re-clone lock is a regular FILE alongside the store (see the script's `ln` note)."""
    return Path(f"{tracker}.reclone.lock")


def _hold_lock(tracker: Path, marker: str) -> Path:
    """Simulate a peer container holding the lock, with `marker` as its recorded timestamp."""
    tracker.mkdir(exist_ok=True)
    lock = _lock_path(tracker)
    lock.write_text(marker)
    return lock


# --------------------------------------------------------------------- poisoned store


def test_poisoned_store_with_dot_git_but_no_resolvable_head_is_cleared_and_recloned(
    tmp_path: Path,
) -> None:
    """The production failure, executed.

    The 120s health rollback removed containers mid-clone, repeatedly. Each removal left the
    PERSISTENT named volume holding orphaned objects, an empty `refs/heads` and HEAD dangling
    at `refs/heads/.invalid`. Because the entrypoint skipped cloning whenever `.git` merely
    EXISTED, every later container inherited that volume, served an empty tracker, and never
    re-cloned. Worse than a missing store: a poisoned one reads as PRESENT, so the
    absent-store guard does not fire and reads return `[]` — a silent empty tracker.
    """
    tracker = tmp_path / "tracker"
    (tracker / ".git" / "refs" / "heads").mkdir(parents=True)
    (tracker / ".git" / "HEAD").write_text("ref: refs/heads/.invalid\n")
    orphan = tracker / ".git" / "objects" / "pack" / "tmp_pack_orphan"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("1.5 GB of nothing")

    run = _provision(tmp_path, head_resolves=False)

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert run.cloned, (
        f"a store with no resolvable HEAD must be re-cloned; git calls={run.git_calls}"
    )
    assert not orphan.exists(), (
        "the directory must be CLEARED before the re-clone, or partial objects survive into it"
    )
    assert "no resolvable HEAD" in run.stderr, "the re-clone must be announced in the logs"
    assert run.returncode == 0


def test_store_with_files_but_no_dot_git_at_all_is_cleared_and_recloned(
    tmp_path: Path,
) -> None:
    """The shape observed in production today: working-tree files, no `.git` whatsoever.

    `git clone` into it fails with "destination path already exists and is not an empty
    directory", so without the clear the store can never self-heal.
    """
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    (tracker / "leftover").write_text("stale working-tree file")

    run = _provision(tmp_path, head_resolves=False)

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert run.cloned, f"git calls={run.git_calls}"
    assert not (tracker / "leftover").exists(), (
        "a non-empty directory must be cleared or `git clone` refuses it outright"
    )


# ---------------------------------------------------------------------- healthy store


def test_healthy_store_is_neither_cleared_nor_recloned(tmp_path: Path) -> None:
    """Idempotent skip: a resolvable HEAD means the store is usable.

    Re-cloning a ~200k-commit branch on every boot would blow straight through the
    blue-green readiness deadline the backgrounding exists to respect.
    """
    tracker = tmp_path / "tracker"
    (tracker / ".git").mkdir(parents=True)
    keep = tracker / "tickets" / "d467.json"
    keep.parent.mkdir()
    keep.write_text("{}")

    run = _provision(tmp_path, head_resolves=True)

    # Liveness FIRST — otherwise the two absence assertions below pass on a script that
    # exited before it ever looked at the store.
    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert any("rev-parse" in call for call in run.git_calls), (
        "the script must actually probe HEAD before deciding to skip"
    )
    assert not run.cloned, f"a healthy store must NOT be re-cloned; git calls={run.git_calls}"
    assert keep.exists(), "a healthy store's contents must survive untouched"
    assert "no resolvable HEAD" not in run.stderr
    assert run.returncode == 0


def test_no_pat_skips_the_clone_but_still_converges_the_store(tmp_path: Path) -> None:
    """Soft failure posture: with no credential there is nothing to clone from, but the
    ensure step must still run so an already-present store stays writable."""
    run = _provision(tmp_path, pat=None, head_resolves=False)

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert not run.cloned, f"git calls={run.git_calls}"
    assert run.returncode == 0


# --------------------------------------------------------------------- failure surfaces


def test_a_failing_clone_surfaces_and_is_not_reported_as_success(tmp_path: Path) -> None:
    """A failed clone must not be silently swallowed into a success.

    Boot is still unaffected — provisioning is backgrounded — but the status has to be
    honest, or a permanently unprovisioned store looks exactly like a healthy one.
    """
    run = _provision(tmp_path, head_resolves=False, clone_fails=True)

    assert run.cloned, "the clone must have been attempted"
    assert run.ran_to_completion, (
        "a failed clone must NOT abort provisioning — the ensure step still runs and the "
        f"terminal line is still emitted: {run.stderr}"
    )
    assert run.returncode != 0, "a failed clone must surface in the exit status"
    assert "clone deferred" in run.stderr


def test_a_failing_ensure_surfaces_in_the_status(tmp_path: Path) -> None:
    """The same honesty for the converge step: an unwritable store is not a provisioned one."""
    run = _provision(tmp_path, head_resolves=True, ensure_fails=True)

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert run.returncode != 0, "a failed ensure must surface in the exit status"
    assert "ensure deferred" in run.stderr


# ------------------------------------------------------------------------- rm -rf guard


@pytest.mark.parametrize("unsafe", ["", "/", "relative/tickets", "   "])
def test_the_clear_refuses_an_unsafe_tracker_dir(tmp_path: Path, unsafe: str) -> None:
    """`rm -rf "$dir"/.[!.]* "$dir"/*` with an empty $dir expands to `rm -rf /*`.

    Nothing blanks the variable today — it is set in the Dockerfile ENV — but the clear is
    one refactor away from catastrophe, so it refuses anything that is not a non-empty
    absolute path other than `/`.
    """
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("must survive")

    run = _provision(tmp_path, tracker_dir=unsafe, head_resolves=False)

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert "refusing to clear" in run.stderr, (
        f"an unsafe REBAR_TRACKER_DIR {unsafe!r} must be refused before any rm"
    )
    assert not run.cloned, f"nothing may be cloned after the refusal; git calls={run.git_calls}"
    assert sentinel.exists(), "the guard must protect the surrounding filesystem"
    assert run.returncode != 0, "a refused clear must surface in the exit status"


def test_a_safe_absolute_tracker_dir_is_accepted(tmp_path: Path) -> None:
    """The guard must not be so strict that it refuses the real configured path."""
    run = _provision(tmp_path, head_resolves=False)

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert "refusing to clear" not in run.stderr
    assert run.cloned


# ----------------------------------------------------------------------- serialization


def test_a_concurrent_reclone_is_not_interleaved(tmp_path: Path) -> None:
    """The named volume is SHARED across containers (blue/green overlap, a restart racing a
    rollout). Two containers can both see HEAD unresolvable; an unserialized rm+clone then
    interleaves — one clears the directory the other is mid-clone into.

    Simulated by pre-creating the lock a peer would hold: this run must decline to clear or
    clone rather than trample the peer's in-flight work.
    """
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    peer_work = tracker / "peer-partial-clone"
    peer_work.write_text("another container is cloning into this directory")
    _hold_lock(tracker, str(int(time.time())))

    run = _provision(tmp_path, head_resolves=False)

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert not run.cloned, (
        f"a container must not clone while a peer holds the lock; git calls={run.git_calls}"
    )
    assert peer_work.exists(), "the peer's in-flight clone must not be cleared out from under it"
    assert "timed out waiting" in run.stderr
    assert run.returncode != 0


def test_an_abandoned_lock_is_broken_rather_than_wedging_the_store_forever(
    tmp_path: Path,
) -> None:
    """A container that dies mid-clone leaves the lock behind on the PERSISTENT volume.

    That is the same class of failure the whole change exists to fix, so the lock must not
    become a second way to poison the store permanently: an old enough lock is broken.
    """
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    lock = _hold_lock(tracker, "1")  # 1970 — unambiguously abandoned

    run = _provision(tmp_path, head_resolves=False, env={"MCP_RECLONE_LOCK_STALE": "60"})

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert "breaking an abandoned" in run.stderr
    assert run.cloned, f"the re-clone must proceed once the stale lock is broken; {run.git_calls}"
    assert not lock.exists(), "the lock must be released when the re-clone finishes"


def test_a_lock_with_an_unreadable_timestamp_is_treated_as_abandoned(tmp_path: Path) -> None:
    """A lock whose marker is garbage must be broken, not trusted forever.

    The marker is written before the lock name exists, so a *partial* marker cannot occur --
    but a corrupted volume, or a marker left by an older build that recorded something other
    than an integer, still must not wedge the store permanently. Anything that does not parse
    as a timestamp is unverifiable age, and unverifiable age is treated as abandoned.
    """
    tracker = tmp_path / "tracker"
    _hold_lock(tracker, "not-a-timestamp")

    run = _provision(tmp_path, head_resolves=False)

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert "breaking an abandoned" in run.stderr, (
        "a lock with an unparseable timestamp must be broken rather than waited on"
    )
    assert run.cloned, f"the re-clone must proceed once the garbage lock is broken; {run.git_calls}"
    assert not _lock_path(tracker).exists(), "the lock must be released when the re-clone finishes"


def test_a_peer_finishing_the_clone_while_we_wait_skips_the_redundant_reclone(
    tmp_path: Path,
) -> None:
    """The double-checked re-probe: whoever loses the race must not clone all over again.

    Both containers saw an unresolvable HEAD before either took the lock, so the loser's
    decision to re-clone is already stale by the time it acquires. Re-cloning ~200k commits a
    second time would burn the readiness budget the backgrounding exists to protect -- and
    would clear the store the winner just populated. So the lock holder re-probes HEAD before
    touching anything.
    """
    tracker = tmp_path / "tracker"
    _hold_lock(tracker, str(int(time.time())))  # a fresh lock: held, not stale
    # `_provision` creates this directory before it starts the subprocess, well before the
    # timer below fires.
    state = tmp_path / "state"

    def peer_finishes() -> None:
        # The winner completes its clone (HEAD now resolves) and releases the lock.
        (state / "head-resolves").touch()
        _lock_path(tracker).unlink()

    timer = threading.Timer(2.0, peer_finishes)
    timer.start()
    try:
        run = _provision(
            tmp_path,
            head_resolves=False,
            env={"MCP_RECLONE_LOCK_POLL": "1", "MCP_RECLONE_LOCK_WAIT": "30"},
        )
    finally:
        timer.cancel()

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    # Liveness for the two absence assertions below: this message is emitted ONLY after the
    # lock was acquired and the re-probe found a healthy store, so it proves the run reached
    # the double-check rather than exiting somewhere earlier.
    assert "was re-cloned by another container" in run.stderr, (
        f"the lock holder must re-probe HEAD before re-cloning: {run.stderr}"
    )
    assert not run.cloned, (
        f"a store the peer already re-cloned must NOT be cloned again; git calls={run.git_calls}"
    )
    assert not _lock_path(tracker).exists(), "the lock must be released on the double-check path"
    assert run.returncode == 0


def test_a_legacy_directory_lock_from_the_previous_build_is_not_spuriously_acquired(
    tmp_path: Path,
) -> None:
    """A FRESH lock left by the previous build must still serialize us out.

    The previous build's lock was a DIRECTORY. `ln FILE DIR` does not fail -- it SUCCEEDS by
    creating the link INSIDE the directory -- so linking against one would hand the lock to
    every container at once and turn serialization silently off, on exactly the upgrade path
    where an old-build container and a new-build container share the volume. The lock must be
    recognised as held and waited on instead.
    """
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    peer_work = tracker / "peer-partial-clone"
    peer_work.write_text("an old-build container is cloning into this directory")
    legacy = _lock_path(tracker)
    legacy.mkdir()
    (legacy / "acquired-at").write_text(str(int(time.time())))  # fresh: genuinely held

    run = _provision(tmp_path, head_resolves=False)

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert "timed out waiting" in run.stderr, (
        f"a fresh old-build lock must be honoured, not linked into: {run.stderr}"
    )
    assert not run.cloned, (
        f"cloning here means the lock was spuriously acquired; git calls={run.git_calls}"
    )
    assert peer_work.exists(), "the old-build peer's in-flight clone must survive"
    assert not (legacy / _lock_path(tracker).name).exists(), (
        "nothing may be linked INSIDE the legacy lock directory"
    )
    assert run.returncode != 0


def test_a_stale_legacy_directory_lock_is_broken_and_the_reclone_proceeds(
    tmp_path: Path,
) -> None:
    """The upgrade must not inherit a permanent wedge from the old lock format either."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    legacy = _lock_path(tracker)
    legacy.mkdir()
    (legacy / "acquired-at").write_text("1")  # 1970 — unambiguously abandoned

    run = _provision(tmp_path, head_resolves=False)

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert "breaking an abandoned" in run.stderr, "a stale old-build lock must be broken"
    assert run.cloned, f"the re-clone must proceed once it is broken; {run.git_calls}"
    assert not legacy.exists(), "the legacy lock directory must be gone"


def test_an_unremovable_stale_lock_exhausts_the_wait_budget_instead_of_spinning(
    tmp_path: Path,
) -> None:
    """The stale-break branch must count against `MCP_RECLONE_LOCK_WAIT`, not loop forever.

    A stale lock whose removal keeps FAILING looks stale again on every single pass. If that
    branch neither sleeps nor advances the wait counter, the loop becomes a hot spin that pins
    a core until the container is killed, with the store never provisioned -- another way to
    turn a recoverable store into a permanently stuck one.
    """
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    legacy = _lock_path(tracker)
    legacy.mkdir()
    (legacy / "acquired-at").write_text("1")  # stale
    # Unwritable lock directory: its entry cannot be unlinked, so the break always fails.
    legacy.chmod(0o555)
    try:
        run = _provision(tmp_path, head_resolves=False)
    finally:
        legacy.chmod(0o755)  # let pytest clean the temp tree up

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert "breaking an abandoned" in run.stderr, "the stale lock must be recognised as stale"
    assert "timed out waiting" in run.stderr, (
        "a stale lock that cannot be removed must exhaust the wait budget rather than spin"
    )
    assert not run.cloned, f"nothing may be cloned without the lock; git calls={run.git_calls}"
    assert run.returncode != 0, "a lock that could never be taken must surface in the status"
