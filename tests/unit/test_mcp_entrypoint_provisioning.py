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

import fcntl
import os
import shutil
import subprocess
import time
from collections.abc import Callable
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
  config)
    [ -f "$GIT_STUB_STATE/config-fails" ] && exit 1
    exit 0
    ;;
  rev-parse)
    # Barrier marker: lets a test wait until the script has actually probed HEAD.
    printf 'x' >> "$GIT_STUB_STATE/rev-parse-seen"
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

# `flock(1)` is util-linux; it is present in the container (python:3.12-slim) and on CI,
# but not on a stock macOS host. Where it is MISSING, shim it faithfully via fcntl so the
# suite runs everywhere. Written into bin_dir only when the real one is absent, so CI and any
# host that has it exercise the REAL utility rather than this stand-in. The shim locks the
# INHERITED fd, so -- exactly like the real thing -- the lock rides the open file description
# the calling shell holds on fd 9 and therefore outlives this process.
_FLOCK_SHIM = """#!/usr/bin/env python3
import fcntl, os, sys, time

argv = sys.argv[1:]
timeout = None
if argv and argv[0] == "-w":
    timeout = float(argv[1])
    argv = argv[2:]
fd = int(argv[0])
if timeout is None:
    fcntl.flock(fd, fcntl.LOCK_EX)
    sys.exit(0)
deadline = time.monotonic() + timeout
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except OSError:
        if time.monotonic() >= deadline:
            sys.exit(1)
        time.sleep(0.05)
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
    config_fails: bool = False,
    env: dict[str, str] | None = None,
    during: Callable[[Path], None] | None = None,
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
    if shutil.which("flock") is None:  # macOS without util-linux; CI uses the real one
        shim = bin_dir / "flock"
        shim.write_text(_FLOCK_SHIM)
        shim.chmod(0o755)

    git_log = tmp_path / "git.log"
    ensure_log = tmp_path / "ensure.log"
    for marker, on in (
        ("head-resolves", head_resolves),
        ("clone-fails", clone_fails),
        ("ensure-fails", ensure_fails),
        ("config-fails", config_fails),
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
            # Keep a contended lock from stalling the suite.
            "MCP_RECLONE_LOCK_WAIT": "2",
            **(env or {}),
        }
    )
    if pat is None:
        proc_env.pop("MCP_TICKETS_PAT", None)
    else:
        proc_env["MCP_TICKETS_PAT"] = pat

    proc = subprocess.Popen(
        ["sh", str(_ENTRYPOINT), "--provision-only"],
        cwd=tmp_path,
        env=proc_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if during is not None:
            during(state)
        _, stderr = proc.communicate(timeout=60)
    except BaseException:
        proc.kill()
        proc.communicate()
        raise

    def _lines(path: Path) -> list[str]:
        return path.read_text().splitlines() if path.exists() else []

    return Run(
        returncode=proc.returncode,
        stderr=stderr,
        git_calls=[
            # Normalise away the `-C <dir>` prefix so assertions read on the subcommand.
            call.split(" ", 2)[2] if call.startswith("-C ") else call
            for call in _lines(git_log)
        ],
        ensure_calls=_lines(ensure_log),
        tracker=tracker,
    )


def _peer_lock_fd(tracker: Path) -> int:
    """Hold the re-clone lock exactly as a peer container does: flock on the store's INODE."""
    tracker.mkdir(exist_ok=True)
    fd = os.open(tracker, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)


def _await_marker(marker: Path, what: str) -> None:
    """Block until the script signals it reached a point. Ordering is enforced by the LOCK,
    not by this wait -- the script cannot pass the lock while the test holds it -- so this
    only bounds how long we sit here, it does not decide the outcome."""
    deadline = time.monotonic() + 30
    while not marker.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"the script never {what}")
        time.sleep(0.01)


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
    rollout, a `restart: always` container coming back with no deploy lock at all). Two can
    both see HEAD unresolvable; an unserialized rm+clone then interleaves -- one clears the
    directory the other is mid-clone into.

    The peer here holds the lock the way a real peer does: an `flock` on the store directory's
    INODE. That is also what pins the lock's LOCATION. The volume is mounted AT the tracker
    directory, so a lock beside it (`<dir>.reclone.lock`) would sit in each container's own
    overlay filesystem -- private per container, serializing nothing. If the script ever locks
    anything other than this inode it will sail past a held lock and clone, and this test
    fails.
    """
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    peer_work = tracker / "peer-partial-clone"
    peer_work.write_text("another container is cloning into this directory")
    fd = _peer_lock_fd(tracker)

    try:
        run = _provision(tmp_path, head_resolves=False)
    finally:
        _release(fd)
        os.close(fd)

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    assert not run.cloned, (
        f"a container must not clone while a peer holds the lock; git calls={run.git_calls}"
    )
    assert peer_work.exists(), "the peer's in-flight clone must not be cleared out from under it"
    assert "timed out waiting" in run.stderr
    assert run.returncode != 0


def test_a_peer_finishing_the_clone_while_we_wait_skips_the_redundant_reclone(
    tmp_path: Path,
) -> None:
    """The double-checked re-probe: whoever loses the race must not clone all over again.

    Both containers saw an unresolvable HEAD before either took the lock, so the loser's
    decision to re-clone is already stale by the time it acquires. Re-cloning ~200k commits a
    second time would burn the readiness budget the backgrounding exists to protect -- and
    would clear the store the winner just populated. So the lock holder re-probes HEAD before
    touching anything.

    The handoff is ordered by the LOCK, not by the clock: the peer holds it before the script
    starts, so the script provably cannot reach the re-probe until the peer releases. The peer
    waits for the script's FIRST HEAD probe (the `rev-parse-seen` marker the git stub writes)
    before publishing a healthy store, so the script is guaranteed to have already decided the
    store was broken -- otherwise it would skip re-cloning for the ordinary reason and this
    test would prove nothing.
    """
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    fd = _peer_lock_fd(tracker)
    released = False

    def peer_finishes(state: Path) -> None:
        nonlocal released
        _await_marker(state / "rev-parse-seen", "probed HEAD before taking the lock")
        (state / "head-resolves").touch()  # the winner's clone completed
        _release(fd)
        released = True

    try:
        run = _provision(tmp_path, head_resolves=False, during=peer_finishes)
    finally:
        if not released:
            _release(fd)
        os.close(fd)

    assert run.ran_to_completion, f"script did not finish: {run.stderr}"
    # Liveness for the absence assertion below: this message is emitted ONLY after the lock
    # was acquired and the re-probe found a healthy store, so it proves the run reached the
    # double-check rather than exiting somewhere earlier.
    assert "was re-cloned by another container" in run.stderr, (
        f"the lock holder must re-probe HEAD before re-cloning: {run.stderr}"
    )
    assert not run.cloned, (
        f"a store the peer already re-cloned must NOT be cloned again; git calls={run.git_calls}"
    )
    assert run.returncode == 0


# ------------------------------------------------------------------ credential helper


def test_a_failing_credential_helper_config_does_not_abort_provisioning(
    tmp_path: Path,
) -> None:
    """`git config` runs as a simple command under `set -e`.

    Unguarded, a non-zero exit kills provision_store outright -- before the ensure step and
    before the terminal log line -- so a credential-helper problem would surface as a silent,
    statusless exit rather than a reported failure. The soft failure posture the whole script
    is built around requires it to be recorded and stepped over instead.
    """
    run = _provision(tmp_path, head_resolves=True, config_fails=True)

    assert run.ran_to_completion, (
        "a failed `git config` must NOT abort provisioning -- the ensure step still runs and "
        f"the terminal line is still emitted: {run.stderr}"
    )
    assert "could not install the tickets credential helper" in run.stderr
    assert run.returncode != 0, "the failure must surface in the exit status"


def test_the_code_root_is_provisioned_so_attested_gates_can_resolve_main(tmp_path: Path) -> None:
    """The entrypoint must provision a CODE repository, not just the tickets store.

    Every attested-source gate — `review_plan`, `verify_completion`, `review_code`,
    `reconcile` — resolves a ref against the repo root. The image cannot supply one:
    `.dockerignore` excludes `.git` and `Dockerfile.mcp` does a plain `COPY . /app`, so `/app`
    is a source copy with no object DB and `origin/main` cannot resolve. The gates fail with
    `cannot resolve ref 'origin/main' to a commit in '.'`, which means NO attestation can be
    earned through the deployed server and no task/story/epic can close through it without a
    force — the one thing agents are forbidden to use.

    `source: "local"` runs today, which is what isolates the fault: the gate CODE is fine, the
    repository is missing. So the fix is to provision one, and this asserts the provisioning
    actually executes rather than that the script contains a line that would.

    The tracker is healthy here (`head_resolves=True`) so no tickets re-clone fires — any
    clone observed is the CODE clone, which keeps the assertion unambiguous.
    """
    code = tmp_path / "code"
    run = _provision(tmp_path, head_resolves=True, env={"MCP_CODE_DIR": str(code)})

    assert run.ran_to_completion, (
        f"precondition: provisioning must run to completion\nrc={run.returncode}\n{run.stderr}"
    )

    main_clones = [c for c in run.git_calls if c.startswith("clone ") and "--branch main" in c]
    assert main_clones, (
        "the entrypoint must clone the CODE side (branch main) so the attested gates have a "
        f"repository to resolve origin/main against. git calls seen: {run.git_calls}\n"
        f"{run.stderr}"
    )
    assert str(code) in main_clones[0], (
        f"the code clone must land in MCP_CODE_DIR ({code}), not somewhere else: {main_clones[0]!r}"
    )
    contents = sorted(p.name for p in code.iterdir()) if code.exists() else "<absent>"
    assert (code / ".git").is_dir(), (
        "MCP_CODE_DIR must end up an actual repository (a .git dir), not a source copy — "
        f"that is the whole defect. contents: {contents}"
    )
