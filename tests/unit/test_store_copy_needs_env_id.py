"""A store materialised WITHOUT `.env-id` rejects library writes until it is converged.

WHY THIS TEST EXISTS. J11's live DC cells run against a copy of the real ticket store, built by
`git archive`-ing the orphan `tickets` branch. That copy is not a usable store: `.env-id` is the
FIRST line of the tickets branch's own `.gitignore`, so the archive cannot contain it, and
`composer.edit_core` (`composer.py:400`) refuses every library write with "ticket system not
initialized". The J11 fixture converges the copy with `run_ensures`; this test pins the mechanism
and the remedy so that call cannot be "simplified" away without a failure that explains itself.

THE TRAP THIS DOCUMENTS, which cost four CI cycles to place. That one message string is emitted
from FOURTEEN sites in `src/`, and the ones that matter here enforce TWO DIFFERENT preconditions:
  * `event_append._ensure_initialized` requires `tracker/.git`;
  * the write seam requires `tracker/.env-id` — `_seam.py:374-381`, the authoritative gate: "every
    write ... flows through here ... guarantees no event is ever appended without an env_id
    provenance stamp ... this is the backstop none can bypass". `composer` (x3), `transition`,
    `claim`, `unlink` and `compact` keep their own EARLY pre-checks emitting the identical string.
The reconciler's store writes go through the `.git` guard and the library's through the `.env-id`
seam, so an archive-materialised copy lets a reconciler pass WRITE SUCCESSFULLY while a library
edit fails on the very same store. That asymmetry looks like a contradiction and is not — and
reasoning from "the reconciler wrote, therefore the store is initialized" is exactly what hid the
real gate. Because the message is duplicated across layers, disabling any single gate does NOT
surface the others; only removing composer's pre-check AND the seam backstop makes the first test
below go red.
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
