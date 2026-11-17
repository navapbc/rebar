"""Dry-run a remote-anchored reclaim end to end on throwaway copies (ADR 0106, story S3).

The harness proves the reclaim on a copy of a real ``tickets`` history before any human
go/no-go on a production swap. It never writes to the source: it fetches one revision
into a fresh repository inside ``--workdir`` and does everything else there.

1. ``t0``: an empty repository that fetches ``--ref`` from ``--source``. Its tip is T0.
2. ``fake-remote.git``: a bare copy of T0 that stands in for the published remote,
   pinned so no detached maintenance races the clones taken from it.
3. ``shadow``: a clone of the fake remote with its remote removed, the disposable-shadow
   marker written and ``refs/remotes/origin/tickets`` at T0, so S1's horizon check passes.
4. The S1 collapse runs at the newest first-parent commit older than the horizon, S2
   stamps a superseding store epoch, and S2 publishes to the fake remote under a T0 lease.
5. The reduced ticket state, ``verify-identity --all`` and the blobless pack size are
   compared before and after, and the result goes to ``<workdir>/evidence.json``.

The command exits 0 only when every check passes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rebar._mcp_errors import js_safe_dumps
from rebar._store.gitutil import run_git

#: The epic's predicted blobless pack size after a 30-day reclaim, in MiB.
PREDICTED_MIB = (365, 380)

_GIT_TIMEOUT_S = 7200.0
_PINS = (
    ("maintenance.auto", "false"),
    ("gc.auto", "0"),
    ("receive.autogc", "false"),
    ("uploadpack.allowFilter", "true"),
)


class DryRunError(RuntimeError):
    """A step failed; the message is recorded as the packet's ``error``."""


@dataclass
class _Packet:
    source: str
    ref: str
    horizon_days: int
    steps: dict[str, float] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)

    def timed(self, name: str, fn: Callable[[], Any]) -> Any:
        started = time.monotonic()
        try:
            return fn()
        finally:
            self.steps[name] = round(time.monotonic() - started, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ref": self.ref,
            "horizon_days": self.horizon_days,
            **self.data,
            "step_seconds": self.steps,
        }


def run_dry_run(
    source: str | os.PathLike[str],
    ref: str,
    workdir: str | os.PathLike[str],
    *,
    horizon_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the dry run and write ``evidence.json``; return the packet.

    Raises :class:`DryRunError` after writing a packet that carries ``error``.
    """
    work = Path(workdir).resolve()
    if work.exists() and any(work.iterdir()):
        raise DryRunError(f"refusing to use non-empty workdir {work}")
    work.mkdir(parents=True, exist_ok=True)
    packet = _Packet(str(source), ref, horizon_days)
    source_before = _resolve(Path(source), ref)
    try:
        _run_steps(packet, Path(source), ref, work, horizon_days, now)
    except (DryRunError, subprocess.SubprocessError, OSError, RuntimeError) as err:
        packet.data["error"] = str(err)
        packet.data["ok"] = False
    finally:
        packet.data["source_ref_unchanged"] = _resolve(Path(source), ref) == source_before
        (work / "evidence.json").write_text(
            json.dumps(packet.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if "error" in packet.data:
        raise DryRunError(packet.data["error"])
    return packet.to_dict()


def _run_steps(
    packet: _Packet,
    source: Path,
    ref: str,
    work: Path,
    horizon_days: int,
    now: datetime | None,
) -> None:
    from rebar._commands import reclaim_collapse, reclaim_publish

    t0_repo, fake, shadow = work / "t0", work / "fake-remote.git", work / "shadow"
    t0 = packet.timed("copy_t0", lambda: _copy_t0(source, ref, t0_repo))
    packet.timed("fake_remote", lambda: _make_fake_remote(t0_repo, fake))
    packet.timed("shadow", lambda: _make_shadow(fake, shadow, t0))
    boundary = _boundary(shadow, t0, horizon_days, now)
    packet.data.update(
        t0=t0,
        boundary=boundary,
        commits_before=_count(shadow, t0),
        predicted_mib=list(PREDICTED_MIB),
    )
    before_state = packet.timed(
        "reduce_before", lambda: reclaim_collapse.reduce_shadow_tracker(shadow)
    )
    pack_before = packet.timed(
        "pack_before", lambda: _blobless_pack_kib(fake, work / "measure-before")
    )

    result = packet.timed(
        "collapse",
        lambda: reclaim_collapse.collapse_shadow_tracker(
            shadow, boundary_commit=boundary, apply=True, now=now
        ),
    )
    epoch = f"{datetime.now(UTC).strftime('%Y-%m-%dT%H-%M-%SZ')}-{uuid.uuid4().hex[:4]}"
    new_tip = reclaim_publish.stamp_superseding_epoch(shadow, epoch)
    swap = packet.timed(
        "publish",
        lambda: reclaim_publish.publish_git_ref(
            shadow, f"file://{fake}", branch="tickets", expected_old_tip=t0, new_tip=new_tip
        ),
    )
    after_state = packet.timed(
        "reduce_after", lambda: reclaim_collapse.reduce_shadow_tracker(shadow)
    )
    identity = packet.timed(
        "verify_identity", lambda: _verify_identity(shadow, result.enforce_since)
    )
    _require_only_tickets_branch(fake)
    pack_after = packet.timed(
        "pack_after", lambda: _blobless_pack_kib(fake, work / "measure-after")
    )

    reducer_equal = before_state == after_state
    published = _resolve_bare(fake, "refs/heads/tickets")
    packet.data.update(
        new_tip=new_tip,
        published_tip=published,
        backup_ref=swap.backup_ref,
        epoch=epoch,
        checkpoint=result.checkpoint_sha,
        enforce_since=result.enforce_since,
        commits_after=_count(shadow, new_tip),
        collapsed_commits=result.collapsed_commits,
        replayed_commits=result.replayed_commits,
        pack_kib_before=pack_before,
        pack_kib_after=pack_after,
        delta_kib=pack_before - pack_after,
        pack_mib_after=round(pack_after / 1024, 1),
        within_prediction=PREDICTED_MIB[0] <= pack_after / 1024 <= PREDICTED_MIB[1],
        reducer_equal=reducer_equal,
        verify_identity_exit=identity[0],
        verify_identity_tail=identity[1],
    )
    problems = []
    if not reducer_equal:
        problems.append("reduced state differs after the rewrite")
    if identity[0] != 0:
        problems.append(f"verify-identity exited {identity[0]}")
    if published != new_tip:
        problems.append(f"fake remote tickets is {published}, expected {new_tip}")
    packet.data["ok"] = not problems
    if problems:
        raise DryRunError("; ".join(problems))


# raw-git-ok: runs only inside the dry-run workdir, or reads (rev-parse) the source
def _git(cwd: Path, *args: str) -> str:
    proc = run_git(cwd, *args, check=False, timeout=_GIT_TIMEOUT_S)
    if proc.returncode != 0:
        raise DryRunError(f"git {' '.join(args[:2])} failed in {cwd}: {proc.stderr.strip()}")
    return proc.stdout.strip()


# raw-git-ok: configures and reads only the throwaway bare repositories in the workdir
def _bare(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise DryRunError(f"git {' '.join(args[:2])} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _resolve(repo: Path, ref: str) -> str | None:
    proc = run_git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _resolve_bare(repo: Path, ref: str) -> str:
    return _bare(repo, "rev-parse", "--verify", ref)


# raw-git-ok: builds a throwaway repository inside the dry-run workdir, never the source
def _copy_t0(source: Path, ref: str, dest: Path) -> str:
    commit = _resolve(source, ref)
    if commit is None:
        raise DryRunError(f"cannot resolve {ref} in {source}")
    dest.mkdir(parents=True)
    _git(dest, "init", "--quiet", "-b", "tickets")
    _git(dest, "fetch", "--quiet", "--no-tags", str(source), f"{commit}:refs/heads/fetched")
    _git(dest, "update-ref", "refs/heads/tickets", "refs/heads/fetched")
    _git(dest, "update-ref", "-d", "refs/heads/fetched")
    return commit


# raw-git-ok: creates and pins the throwaway fake remote inside the dry-run workdir
def _make_fake_remote(t0_repo: Path, fake: Path) -> None:
    _git(t0_repo, "clone", "--quiet", "--bare", "--no-local", str(t0_repo), str(fake))
    for key, value in _PINS:
        _bare(fake, "config", key, value)
    _require_only_tickets_branch(fake)


def _require_only_tickets_branch(fake: Path) -> None:
    """A second branch would keep pre-reclaim history reachable and skew the measurement."""
    heads = _bare(fake, "for-each-ref", "--format=%(refname)", "refs/heads/").split()
    if heads != ["refs/heads/tickets"]:
        raise DryRunError(f"fake remote must hold only refs/heads/tickets, found {heads}")


# raw-git-ok: builds the disposable shadow clone inside the dry-run workdir
def _make_shadow(fake: Path, shadow: Path, t0: str) -> None:
    _git(
        fake.parent, "clone", "--quiet", "--no-local", "--branch", "tickets", str(fake), str(shadow)
    )
    _git(shadow, "remote", "remove", "origin")
    marker = shadow / ".rebar" / "reclaim-shadow-clone"
    marker.parent.mkdir(exist_ok=True)
    marker.write_text("disposable dry-run shadow\n", encoding="utf-8")
    _git(shadow, "update-ref", "refs/remotes/origin/tickets", t0)


def _boundary(shadow: Path, t0: str, horizon_days: int, now: datetime | None) -> str:
    moment = now or datetime.now(UTC)
    cutoff = (moment - timedelta(days=horizon_days)).strftime("%Y-%m-%d %H:%M:%S %z")
    found = _git(shadow, "rev-list", "-1", "--first-parent", f"--before={cutoff}", t0)
    if not found:
        raise DryRunError(f"no eligible boundary: no first-parent commit before {cutoff}")
    return found


def _count(repo: Path, rev: str) -> int:
    return int(_git(repo, "rev-list", "--count", rev))


# raw-git-ok: clones the throwaway fake remote into a throwaway measuring repository
def _blobless_pack_kib(fake: Path, dest: Path) -> int:
    _git(
        fake.parent,
        "clone",
        "--quiet",
        "--bare",
        "--filter=blob:none",
        f"file://{fake}",
        str(dest),
    )
    stats = dict(line.split(": ", 1) for line in _bare(dest, "count-objects", "-v").splitlines())
    return int(stats.get("size-pack", "0"))


def _verify_identity(shadow: Path, enforce_since: str) -> tuple[int, str]:
    env = dict(os.environ)
    env.update(
        REBAR_ROOT=str(shadow.parent),
        REBAR_TRACKER_DIR=str(shadow),
        REBAR_IDENTITY_REQUIRE_AUTHENTICATED="1",
        REBAR_IDENTITY_ENFORCE_SINCE=enforce_since,
    )
    proc = subprocess.run(
        [sys.executable, "-m", "rebar", "verify-identity", "--all"],
        cwd=shadow.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-20:]
    return proc.returncode, "\n".join(tail)


def main(argv: list[str] | None = None) -> int:
    from rebar._cli._parser import build_argument_parser

    parser = build_argument_parser(
        prog="python -m rebar._commands.reclaim_dry_run",
        description="Dry-run a reclaim on throwaway copies and write evidence.json.",
    )
    parser.add_argument("--source", required=True, help="repository holding the tickets history")
    parser.add_argument("--ref", required=True, help="revision of the tickets history to copy")
    parser.add_argument("--workdir", required=True, help="empty directory for every copy")
    parser.add_argument("--horizon-days", type=int, default=30, help="reclaim horizon")
    args = parser.parse_args(argv)
    try:
        packet = run_dry_run(args.source, args.ref, args.workdir, horizon_days=args.horizon_days)
    except DryRunError as err:
        print(f"reclaim dry run failed: {err}", file=sys.stderr)
        return 1
    print(js_safe_dumps(packet, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
