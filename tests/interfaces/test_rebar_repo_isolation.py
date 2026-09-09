"""Sentinels for the ``rebar_repo`` copy-isolation contract (ticket 699f).

The optimized fixture copies one per-worker template. A copy that is not
re-pointed can look correct while sharing the template's object database and
``refs/heads/tickets``, so its writes corrupt siblings. These tests construct broken
copies and assert topology and refs directly.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from _git_upkeep import assert_no_detached_upkeep, init_bare_remote
from _store_template import (
    _IDENTITY_FILES,
    _WORKTREE_POINTERS,
    _clone_template,
    assert_store_self_contained,
    worktree_paths,
)
from _subprocess_env import subprocess_env

import rebar
from rebar import signing


def _rewrite(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def _tickets_ref(repo: Path) -> str:
    """The store's ``refs/heads/tickets`` sha, read through the copy's own gitdir."""
    return subprocess.run(
        ["git", "-C", str(repo / ".tickets-tracker"), "rev-parse", "refs/heads/tickets"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_fully_fixed_copy_is_self_contained(rebar_repo: Path) -> None:
    """The real fixture output must pass the guard (baseline: the guard is satisfiable)."""
    assert_store_self_contained(rebar_repo)
    root = rebar_repo.resolve()
    for p in worktree_paths(rebar_repo):
        assert root in (p.resolve(), *p.resolve().parents)


def test_half_fixed_copy_is_rejected(_rebar_repo_template: Path, tmp_path: Path) -> None:
    """Reject a copy whose worktree ``.git`` changed but ``gitdir`` stayed stale.

    ``rev-parse --git-common-dir`` passes on this broken shape, so only the
    worktree topology exposes it.
    """
    dest = tmp_path / "half"
    shutil.copytree(_rebar_repo_template, dest, symlinks=True)
    src_s, dst_s = str(_rebar_repo_template.resolve()), str(dest.resolve())
    _rewrite(dest / _WORKTREE_POINTERS[0], src_s, dst_s)  # .tickets-tracker/.git only
    # gitdir deliberately left stale.

    # The weaker check that MUST NOT be used: it is green on this broken store.
    common = subprocess.run(
        ["git", "-C", str(dest / ".tickets-tracker"), "rev-parse", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert dst_s in common, "precondition: --git-common-dir is green on a half-fixed copy"

    with pytest.raises(AssertionError, match="outside itself"):
        assert_store_self_contained(dest)


def test_bare_worktree_repair_is_destructive_and_is_rejected(
    _rebar_repo_template: Path, tmp_path: Path
) -> None:
    """Prove bare worktree repair leaves the copy broken and corrupts its source.

    Repair rewrites the source tracker pointer toward the copy, so this test uses a
    private sacrificial source instead of the shared session template.
    """
    sacrificial = tmp_path / "sacrificial"
    shutil.copytree(_rebar_repo_template, sacrificial, symlinks=True)
    src_s, sac_s = str(_rebar_repo_template.resolve()), str(sacrificial.resolve())
    for rel in _WORKTREE_POINTERS:
        p = sacrificial / rel
        if p.exists():
            _rewrite(p, src_s, sac_s)
    assert_store_self_contained(sacrificial)

    dest = tmp_path / "repaired"
    shutil.copytree(sacrificial, dest, symlinks=True)
    subprocess.run(["git", "-C", str(dest), "worktree", "repair"], check=True, capture_output=True)

    # Repair leaves the copy linked to its source.
    with pytest.raises(AssertionError, match="outside itself"):
        assert_store_self_contained(dest)

    # It also redirects the source tracker into the copy.
    assert str(dest.resolve()) in (sacrificial / _WORKTREE_POINTERS[0]).read_text(
        encoding="utf-8"
    ), "expected bare repair to redirect the SOURCE store into the copy"


def test_copy_rejects_shared_git_object_alternates(
    _rebar_repo_template: Path, tmp_path: Path
) -> None:
    """A reference clone must not keep reading another topology's objects."""
    source = _clone_template(_rebar_repo_template, tmp_path / "reference-source")
    shared_objects = tmp_path / "shared-objects"
    shared_objects.mkdir()
    alternates = source / ".git/objects/info/alternates"
    alternates.write_text(f"{shared_objects.resolve()}\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="object alternates"):
        _clone_template(source, tmp_path / "reference-copy")


def test_no_file_in_a_copy_contains_the_template_path(
    _rebar_repo_template: Path, rebar_repo: Path
) -> None:
    """Catch absolute template paths beyond the known pointer list."""
    needle = str(_rebar_repo_template.resolve())
    offenders = []
    for p in rebar_repo.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        try:
            if needle in p.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(p.relative_to(rebar_repo)))
        except OSError:
            continue
    assert not offenders, f"copy still references the template path in: {offenders}"


def test_write_in_one_store_does_not_move_another_stores_ref(
    _rebar_repo_template: Path, rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove a copy write advances only its own ticket ref.

    File-based reads can hide a shared object store, so compare refs directly.
    """
    template_before = _tickets_ref(_rebar_repo_template)

    monkeypatch.setenv("REBAR_ROOT", str(rebar_repo))
    monkeypatch.chdir(rebar_repo)
    rebar.create_ticket("task", "isolation probe", return_alias=True)

    assert _tickets_ref(rebar_repo) != template_before, "the copy's own ref must advance"
    assert _tickets_ref(_rebar_repo_template) == template_before, (
        "writing to a copy moved the TEMPLATE's refs/heads/tickets — the copy is "
        "sharing the template's object database"
    )


def test_identity_is_reminted_per_store(_rebar_repo_template: Path, rebar_repo: Path) -> None:
    """Require fresh store identity files instead of copied template values.

    The guarded ``init._gen_local_files()`` helper would leave existing copies
    unchanged, so cloning must overwrite them.
    """
    for rel, _mode in _IDENTITY_FILES:
        template_val = (_rebar_repo_template / rel).read_text(encoding="utf-8").strip()
        copy_val = (rebar_repo / rel).read_text(encoding="utf-8").strip()
        assert copy_val != template_val, f"{rel} was not re-minted (still the template's)"
        assert uuid.UUID(copy_val), f"{rel} is not a uuid: {copy_val!r}"


def test_signing_key_keeps_restrictive_mode(rebar_repo: Path) -> None:
    """Re-minting must not widen the signing key's permissions."""
    mode = (rebar_repo / ".tickets-tracker/.signing-key").stat().st_mode & 0o777
    assert mode == 0o600, f"signing key mode widened to {oct(mode)}"


def test_opcert_signing_key_is_isolated_per_store(
    _rebar_repo_template: Path, tmp_path: Path
) -> None:
    """A copied store must never inherit another environment's private key."""
    template_tracker = _rebar_repo_template / ".tickets-tracker"
    template_key = Path(signing.ensure_opcert_key(template_tracker))
    template_private = template_key.read_bytes()
    template_public = template_key.with_suffix(".pub").read_bytes()

    copies = [
        _clone_template(_rebar_repo_template, tmp_path / name) for name in ("opcert-a", "opcert-b")
    ]
    copy_keys = [Path(signing.ensure_opcert_key(copy / ".tickets-tracker")) for copy in copies]

    assert template_key.read_bytes() == template_private
    assert template_key.with_suffix(".pub").read_bytes() == template_public
    assert len({template_private, *(key.read_bytes() for key in copy_keys)}) == 3
    assert len({template_public, *(key.with_suffix(".pub").read_bytes() for key in copy_keys)}) == 3
    assert all((key.stat().st_mode & 0o777) == 0o600 for key in copy_keys)


def test_two_stores_are_mutually_independent(
    _rebar_repo_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two copies taken from one template must not see each other's tickets."""
    a = _clone_template(_rebar_repo_template, tmp_path / "a")
    b = _clone_template(_rebar_repo_template, tmp_path / "b")

    monkeypatch.setenv("REBAR_ROOT", str(a))
    monkeypatch.chdir(a)
    rebar.create_ticket("task", "only in A", return_alias=True)

    monkeypatch.setenv("REBAR_ROOT", str(b))
    monkeypatch.chdir(b)
    assert rebar.list_tickets() == [], "store B saw store A's writes"


def test_template_stays_virgin(_rebar_repo_template: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the template ticket-free. Interface empty-state tests depend on it."""
    monkeypatch.setenv("REBAR_ROOT", str(_rebar_repo_template))
    monkeypatch.chdir(_rebar_repo_template)
    assert rebar.list_tickets() == [], "the template has been seeded with tickets"
    assert not (_rebar_repo_template / ".rebar").exists(), "template carries .rebar state"


def test_fixture_bare_remote_leaves_no_detached_upkeep_behind_a_push(
    _rebar_repo_template: Path,
    tmp_path: Path,
) -> None:
    """Prove fixture pushes leave no detached Git maintenance process (bug dca1).

    The next test copies the remote immediately after a push. Trace argv directly
    and reject ``git maintenance run --detach`` instead of relying on timing.
    """
    trace = tmp_path / "trace2.json"
    remote = init_bare_remote(tmp_path / "origin.git")
    work = _clone_template(_rebar_repo_template, tmp_path / "work")
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "origin", "HEAD:tickets"],
        check=True,
        capture_output=True,
        text=True,
        env=subprocess_env(GIT_TRACE2_EVENT=str(trace)),
    )

    assert_no_detached_upkeep(trace, remote)


def test_copy_repoints_sibling_origin_and_keeps_refs_isolated(
    _rebar_repo_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copied store must push only to the matching copied sibling origin."""
    source = tmp_path / "source"
    source.mkdir()
    source_origin = init_bare_remote(source / "origin.git")
    source_work = _clone_template(_rebar_repo_template, source / "work")
    subprocess.run(
        ["git", "-C", str(source_work), "remote", "add", "origin", str(source_origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    upstream_url = "https://example.invalid/rebar.git"
    subprocess.run(
        ["git", "-C", str(source_work), "remote", "add", "upstream", upstream_url],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(source_work), "push", "-q", "origin", "HEAD:tickets"],
        check=True,
        capture_output=True,
        text=True,
    )

    destination = tmp_path / "destination"
    destination.mkdir()
    destination_origin = destination / "origin.git"
    shutil.copytree(source_origin, destination_origin, symlinks=True)
    destination_work = _clone_template(source_work, destination / "work")

    copied_origin = subprocess.run(
        ["git", "-C", str(destination_work), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(copied_origin).resolve() == destination_origin.resolve(), (
        "the copied store still pushes to its template topology's origin"
    )
    copied_upstream = subprocess.run(
        ["git", "-C", str(destination_work), "remote", "get-url", "upstream"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert copied_upstream == upstream_url, "non-local remotes must remain unchanged"

    source_before = _tickets_ref(source_work)
    destination_before = _tickets_ref(destination_work)
    assert destination_before == source_before, "precondition: copied ticket refs initially match"

    monkeypatch.setenv("REBAR_ROOT", str(destination_work))
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "destination-gate"))
    monkeypatch.chdir(destination_work)
    rebar.create_ticket("task", "destination-only ticket", return_alias=True)

    assert _tickets_ref(destination_work) != destination_before, (
        "the destination ticket ref did not advance"
    )
    assert _tickets_ref(source_work) == source_before, (
        "writing to the destination moved the source ticket ref"
    )

    source_root = str(source.resolve()).encode()
    offenders = [
        str(path.relative_to(destination))
        for path in destination.rglob("*")
        if path.is_file() and not path.is_symlink() and source_root in path.read_bytes()
    ]
    assert not offenders, f"destination still embeds source topology paths: {offenders}"
