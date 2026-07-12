"""Happy-path oracle for 4e9d (epic gnu-whale-ichor): event author attribution.

The ONLY 4e9d test the implementation sees. Pins the public contract on
well-formed input: every locally-written event's envelope carries a denormalized
`author_email`, and an optional `author_id` when a current identity resolves.
Edge/all-composer/back-compat/cache behaviour is validated separately (held out).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir

GIT_EMAIL = "dev@example.com"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", GIT_EMAIL),
        ("git", "config", "user.name", "Dev Example"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _events(repo: Path, tid: str, etype: str) -> list[dict]:
    """Raw stored event envelopes of ``etype`` for ``tid`` (chronological)."""
    tdir = Path(tracker_dir(str(repo))) / tid
    out = []
    for name in sorted(os.listdir(tdir)):
        if name.endswith(f"-{etype}.json") and not name.startswith("."):
            out.append(json.loads((tdir / name).read_text()))
    return out


def test_create_event_carries_author_email(store: Path) -> None:
    """Every mutating event's envelope carries a denormalized author email."""
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    (create,) = _events(store, tid, "CREATE")
    assert create["author_email"] == GIT_EMAIL


def test_author_id_present_when_identity_set(store: Path) -> None:
    """With a current identity, the envelope carries author_id == that identity."""
    ident = rebar.create_identity("Dev", GIT_EMAIL, repo_root=str(store))
    rebar.use_identity(ident, repo_root=str(store))
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    (create,) = _events(store, tid, "CREATE")
    assert create["author_id"] == ident
    assert create["author_email"] == GIT_EMAIL


def test_author_id_absent_when_no_identity(store: Path) -> None:
    """With no resolvable identity, the author_id key is omitted entirely."""
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    (create,) = _events(store, tid, "CREATE")
    assert "author_id" not in create
