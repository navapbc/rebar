"""Git-ref publish-swap, its T0 lease, and rollback re-convergence (ADR 0106 §6, story S2).

Every repository is a throwaway under ``tmp_path``: a bare "remote", a writer clone that
moves it, a disposable shadow clone that publishes, and client clones that reconverge
through :func:`rebar._store.sync._do_reconverge`. No real ``tickets`` remote is touched.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _git_upkeep import init_bare_remote

from rebar._commands import reclaim_publish as publish_swap
from rebar._store import compat, sync

pytestmark = pytest.mark.unit

OLD = "2026-10-01T00-00-00Z-old0"
NEW = "2026-11-10T00-00-00Z-new0"
BACK = "2026-11-11T00-00-00Z-back"
TICKET = "aaaa-1111-2222-3333"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")


def _clone(origin: Path, dest: Path) -> Path:
    subprocess.run(
        ["git", "clone", "-q", "-b", "tickets", str(origin), str(dest)],
        check=True,
        capture_output=True,
    )
    _identity(dest)
    return dest


def _event(repo: Path, n: int, when: datetime) -> str:
    rel = f"{TICKET}/{int(when.timestamp() * 1e9)}-{n:08x}-0000-4000-8000-{n:012x}-COMMENT.json"
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(json.dumps({"n": n}) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", f"event {n}")
    return rel


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path | str]:
    """A bare remote at T0 (epoch OLD), a writer clone, and a shadow holding a rewrite."""
    origin = init_bare_remote(tmp_path / "origin.git", initial_branch="tickets")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "tickets")
    _identity(seed)
    record = {"epoch": OLD, "format_version": 1, "required_capabilities": []}
    (seed / compat.COMPAT_FILENAME).write_text(json.dumps(record) + "\n", encoding="utf-8")
    _event(seed, 1, datetime.now(UTC) - timedelta(days=90))
    _git(seed, "push", "-q", str(origin), "tickets:tickets")
    t0 = _git(seed, "rev-parse", "HEAD")

    shadow = _clone(origin, tmp_path / "shadow")
    _git(shadow, "remote", "remove", "origin")
    (shadow / ".rebar").mkdir()
    (shadow / ".rebar" / "reclaim-shadow-clone").write_text("disposable\n", encoding="utf-8")
    # Stand-in for the S1 collapse: a parentless commit carrying T0's exact tree.
    collapsed = _git(shadow, "commit-tree", f"{t0}^{{tree}}", "-m", "checkpoint")
    _git(shadow, "reset", "-q", "--hard", collapsed)
    new_tip = publish_swap.stamp_superseding_epoch(shadow, NEW)
    return {"origin": origin, "shadow": shadow, "t0": t0, "new_tip": new_tip, "tmp": tmp_path}


def _bare_git(repo: Path, *args: str) -> str:
    # A bare repo must be NAMED by --git-dir: git refuses to discover one via -C under
    # safe.bareRepository=explicit.
    proc = subprocess.run(
        ["git", "--git-dir", str(repo), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _remote_ref(origin: Path, ref: str) -> str:
    return _bare_git(origin, "rev-parse", "--verify", "--quiet", ref)


def _backup_refs(origin: Path) -> list[str]:
    out = _bare_git(
        origin, "for-each-ref", "--format=%(refname) %(objectname)", "refs/tickets-backup/"
    )
    return [line for line in out.splitlines() if line]


def _reconverge(clone: Path) -> None:
    _git(clone, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")
    sync._do_reconverge(str(clone), "tickets", "origin")


def test_stamp_records_the_replaced_epoch_in_the_lineage(world) -> None:
    shown = _git(world["shadow"], "show", f"{world['new_tip']}:{compat.COMPAT_FILENAME}")
    record = json.loads(shown)
    assert record["epoch"] == NEW
    assert record["epoch_lineage"] == [OLD]
    assert _git(world["shadow"], "rev-list", "--count", world["new_tip"]) == "2"


def test_git_ref_swap_replaces_branch_and_backs_up_t0(world) -> None:
    origin, t0, new_tip = world["origin"], world["t0"], world["new_tip"]

    result = publish_swap.publish_git_ref(
        world["shadow"], str(origin), branch="tickets", expected_old_tip=t0, new_tip=new_tip
    )

    assert _remote_ref(origin, "refs/heads/tickets") == new_tip
    assert result.backup_ref.startswith(publish_swap.BACKUP_REF_PREFIX)
    assert _backup_refs(origin) == [f"{result.backup_ref} {t0}"]


def test_git_ref_swap_aborts_without_writing_when_remote_moved(world) -> None:
    origin = world["origin"]
    writer = _clone(origin, world["tmp"] / "writer")
    _event(writer, 2, datetime.now(UTC))
    _git(writer, "push", "-q", "origin", "tickets")
    moved = _remote_ref(origin, "refs/heads/tickets")

    with pytest.raises(publish_swap.PublishAborted):
        publish_swap.publish_git_ref(
            world["shadow"],
            str(origin),
            branch="tickets",
            expected_old_tip=world["t0"],
            new_tip=world["new_tip"],
        )

    assert _remote_ref(origin, "refs/heads/tickets") == moved
    assert _backup_refs(origin) == []


def test_publish_refuses_a_non_shadow_checkout(world) -> None:
    plain = _clone(world["origin"], world["tmp"] / "plain")
    with pytest.raises(Exception, match="shadow"):
        publish_swap.publish_git_ref(
            plain,
            str(world["origin"]),
            branch="tickets",
            expected_old_tip=world["t0"],
            new_tip=world["t0"],
        )


def test_rollback_reconverges_clients_on_either_history(world) -> None:
    origin, shadow, t0, new_tip = world["origin"], world["shadow"], world["t0"], world["new_tip"]
    # A client that never moved, with a fresh unpushed write (shares history with T0).
    stayed = _clone(origin, world["tmp"] / "stayed")
    pending = _event(stayed, 3, datetime.now(UTC))

    publish_swap.publish_git_ref(
        shadow, str(origin), branch="tickets", expected_old_tip=t0, new_tip=new_tip
    )
    # A client that reconciled onto the rewritten baseline BEFORE the rollback.
    moved = _clone(origin, world["tmp"] / "moved")
    assert _git(moved, "rev-parse", "HEAD") == new_tip

    # Roll back: restore T0's history under an epoch that supersedes both OLD and NEW.
    _git(shadow, "reset", "-q", "--hard", t0)
    restored = publish_swap.stamp_superseding_epoch(shadow, BACK, replaced_tip=new_tip)
    publish_swap.rollback_git_ref(
        shadow, str(origin), branch="tickets", expected_current_tip=new_tip, restored_tip=restored
    )
    assert _remote_ref(origin, "refs/heads/tickets") == restored
    lineage = json.loads(_git(shadow, "show", f"{restored}:{compat.COMPAT_FILENAME}"))
    assert lineage["epoch_lineage"] == [OLD, NEW]

    _reconverge(moved)
    assert _git(moved, "rev-parse", "HEAD") == restored

    _reconverge(stayed)
    assert _git(stayed, "merge-base", "--is-ancestor", restored, "HEAD") == ""
    assert (stayed / pending).exists()


def test_rollback_aborts_when_the_rewritten_tip_moved(world) -> None:
    origin, shadow, t0, new_tip = world["origin"], world["shadow"], world["t0"], world["new_tip"]
    publish_swap.publish_git_ref(
        shadow, str(origin), branch="tickets", expected_old_tip=t0, new_tip=new_tip
    )
    writer = _clone(origin, world["tmp"] / "writer")
    _event(writer, 4, datetime.now(UTC))
    _git(writer, "push", "-q", "origin", "tickets")
    moved = _remote_ref(origin, "refs/heads/tickets")

    with pytest.raises(publish_swap.PublishAborted):
        publish_swap.rollback_git_ref(
            shadow, str(origin), branch="tickets", expected_current_tip=new_tip, restored_tip=t0
        )

    assert _remote_ref(origin, "refs/heads/tickets") == moved
