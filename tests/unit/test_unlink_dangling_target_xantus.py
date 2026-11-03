"""``unlink`` removes a link whose stored target is not a ticket (lionly-squarish-xantus).

The reducer keeps a LINK's raw ``target_id`` when it does not resolve (``process_link``), and
an UNLINK cancels by ``link_uuid``. ``unlink_core`` used to resolve the target first and refuse
with "does not exist", so a dangling edge (for example a ``caused_by`` written with an ambiguous
4-hex quad) could not be removed through any supported command. The fallback matches the
given target against the stored raw string exactly, and refuses rather than guess when the
match spans more than one relation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar import RebarError, config
from rebar._commands._seam import append_event

pytestmark = pytest.mark.unit


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: str) -> Path:
    return Path(config.tracker_dir(repo))


def _shared_quad(repo: str) -> tuple[str, str, str]:
    """Two tickets sharing a first quad, so that quad resolves to nothing."""
    origin = rebar.create_ticket("task", "the epic", repo_root=repo)
    twin = rebar.create_ticket("task", "unrelated twin", repo_root=repo)
    twin_id = f"{origin[:4]}-{twin[5:]}"
    os.rename(_tracker(repo) / twin, _tracker(repo) / twin_id)
    return origin[:4], origin, twin_id


def _raw_link(repo: str, source: str, target: str, relation: str) -> None:
    """Write a LINK with a raw target, the way the pre-fix close path did."""
    append_event(
        source,
        "LINK",
        {"target_id": target, "relation": relation},
        _tracker(repo),
        repo_root=repo,
    )


def _deps(ticket: str, repo: str) -> list[tuple[str, str]]:
    deps = rebar.show_ticket(ticket, repo_root=repo)["deps"]
    return sorted((d["target_id"], d["relation"]) for d in deps)


def _link_uuid(ticket: str, repo: str, target: str, relation: str) -> str:
    deps = rebar.show_ticket(ticket, repo_root=repo)["deps"]
    (uuid,) = [
        d["link_uuid"] for d in deps if d["target_id"] == target and d["relation"] == relation
    ]
    return uuid


def _unlink_event_uuids(ticket: str, repo: str) -> list[str]:
    import json

    return [
        json.loads(p.read_text(encoding="utf-8"))["data"]["link_uuid"]
        for p in sorted((_tracker(repo) / ticket).glob("*-UNLINK.json"))
    ]


def test_library_unlink_removes_caused_by_with_ambiguous_quad_target(rebar_repo) -> None:
    repo = str(rebar_repo)
    quad, _origin, _twin = _shared_quad(repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    _raw_link(repo, bug, quad, "caused_by")
    uuid = _link_uuid(bug, repo, quad, "caused_by")

    rebar.unlink(bug, quad, "caused_by", repo_root=repo)

    assert (quad, "caused_by") not in _deps(bug, repo)
    assert _unlink_event_uuids(bug, repo) == [uuid]


def test_cli_unlink_removes_caused_by_with_ambiguous_quad_target(rebar_repo) -> None:
    repo = str(rebar_repo)
    quad, _origin, _twin = _shared_quad(repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    _raw_link(repo, bug, quad, "caused_by")

    proc = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "unlink", bug, quad, "caused_by"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "does not resolve" in proc.stderr
    assert (quad, "caused_by") not in _deps(bug, repo)


def test_unknown_full_id_target_is_removed_without_a_relation(rebar_repo) -> None:
    repo = str(rebar_repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    ghost = "0000-1111-2222-3333"
    _raw_link(repo, bug, ghost, "relates_to")

    rebar.unlink(bug, ghost, repo_root=repo)

    assert _deps(bug, repo) == []


def test_link_to_a_real_ticket_sharing_the_quad_survives(rebar_repo) -> None:
    repo = str(rebar_repo)
    quad, origin, _twin = _shared_quad(repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    rebar.link(bug, origin, "discovered_from", repo_root=repo)
    _raw_link(repo, bug, quad, "caused_by")

    rebar.unlink(bug, quad, repo_root=repo)

    assert _deps(bug, repo) == [(origin, "discovered_from")]


def test_dangling_links_across_relations_without_a_relation_are_refused(rebar_repo) -> None:
    repo = str(rebar_repo)
    quad, _origin, _twin = _shared_quad(repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    _raw_link(repo, bug, quad, "caused_by")
    _raw_link(repo, bug, quad, "relates_to")
    before = _deps(bug, repo)

    with pytest.raises(RebarError) as excinfo:
        rebar.unlink(bug, quad, repo_root=repo)

    message = str(excinfo.value)
    assert "caused_by" in message and "relates_to" in message
    assert _deps(bug, repo) == before
    assert _unlink_event_uuids(bug, repo) == []


def test_dangling_relation_selector_removes_only_that_relation(rebar_repo) -> None:
    repo = str(rebar_repo)
    quad, _origin, _twin = _shared_quad(repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    _raw_link(repo, bug, quad, "caused_by")
    _raw_link(repo, bug, quad, "relates_to")

    rebar.unlink(bug, quad, "caused_by", repo_root=repo)

    assert _deps(bug, repo) == [(quad, "relates_to")]


def test_unresolvable_target_with_no_stored_link_still_fails(rebar_repo) -> None:
    repo = str(rebar_repo)
    quad, _origin, _twin = _shared_quad(repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)

    with pytest.raises(RebarError) as excinfo:
        rebar.unlink(bug, quad, "caused_by", repo_root=repo)

    assert "does not exist" in str(excinfo.value)
    assert _unlink_event_uuids(bug, repo) == []
