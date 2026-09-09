"""Require convergence before library writes to a store copied without ``.env-id``.

J11 archives the orphan ``tickets`` branch, whose ignored ``.env-id`` cannot enter the copy;
``run_ensures`` must restore that identity before library use. Two similarly worded guards are
intentionally distinct: ``event_append._ensure_initialized`` requires ``tracker/.git``, while
the write seam requires ``tracker/.env-id`` so no event lacks environment provenance. Thus a
reconciler can write through the Git guard while a library edit correctly fails at the identity
backstop. The tests pin both composer's early check and the authoritative seam check despite
their shared error text.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar


@pytest.fixture
def converged_store(tmp_path: Path) -> tuple[Path, str]:
    """An initialized store holding one real ticket — `(repo_root, ticket_id)`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # `init_repo` creates the store as an ORPHAN WORKTREE, so the enclosing directory has to be
    # a git repo first (`git worktree add --orphan` fails otherwise) and needs a committer
    # identity, which a CI runner has no global copy of.
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    rebar.init_repo(repo_root=repo)
    created = rebar.create_ticket("task", "env-id gate probe", repo_root=repo)
    ticket_id = created["id"] if isinstance(created, dict) else str(created)
    return repo, ticket_id


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def test_removing_env_id_makes_library_writes_fail(converged_store) -> None:
    """Deleting `.env-id` reproduces exactly what `git archive` hands us.

    This is the mechanism, isolated: the ONLY difference from a working store is the missing
    marker, so a failure here cannot be attributed to anything else.
    """
    repo, ticket_id = converged_store
    env_id = _tracker(repo) / ".env-id"
    assert env_id.is_file(), "precondition: a freshly initialized store HAS the marker"

    env_id.unlink()

    with pytest.raises(rebar.RebarError) as excinfo:
        rebar.edit_ticket(ticket_id, repo_root=repo, title="should be refused")
    assert "not initialized" in str(excinfo.value), (
        f"expected the store-marker gate to refuse the write; got {excinfo.value!r}"
    )


def test_ensure_registry_reconverges_a_store_missing_env_id(converged_store) -> None:
    """`run_ensures` restores the marker and writes succeed again — the sanctioned remedy.

    Asserts the OBSERVABLE outcome (the write lands and the title changes), not that a
    particular ensure ran, so the test survives a refactor of the ensure registry itself.
    """
    from rebar._store.ensures import run_ensures

    repo, ticket_id = converged_store
    (_tracker(repo) / ".env-id").unlink()

    for _outcome in run_ensures(str(_tracker(repo))):
        pass

    assert (_tracker(repo) / ".env-id").is_file(), "ensure-registry did not restore the marker"

    rebar.edit_ticket(ticket_id, repo_root=repo, title="accepted after reconvergence")
    assert rebar.show_ticket(ticket_id, repo_root=repo)["title"] == "accepted after reconvergence"
