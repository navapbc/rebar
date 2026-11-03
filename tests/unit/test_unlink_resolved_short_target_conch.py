"""``unlink`` removes a stored short-target link that resolves (jovial-subcranial-conch).

``process_link`` resolves a LINK's stored ``target_id`` at replay, so ``show`` lists a link
written with a unique 4-hex quad under the full ticket id. ``unlink`` compared the stored raw
string against the resolved input, so neither the quad nor the full id matched and the link
could not be removed. A stored target now matches when it resolves to the same ticket as the
input. A stored target that is ambiguous, or that resolves to a different ticket, never
matches. The snapshot cases cover links folded into a ``SNAPSHOT`` by compaction, for both the
resolved path and the exact-string dangling path (lionly-squarish-xantus).
"""

from __future__ import annotations

import json
import os
import subprocess
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


def _make_twin(repo: str, origin: str) -> str:
    """Create a ticket sharing ``origin``'s first quad, so that quad becomes ambiguous."""
    twin = rebar.create_ticket("task", "unrelated twin", repo_root=repo)
    twin_id = f"{origin[:4]}-{twin[5:]}"
    os.rename(_tracker(repo) / twin, _tracker(repo) / twin_id)
    return twin_id


def _raw_link(repo: str, source: str, target: str, relation: str) -> str:
    """Write a LINK with a raw target, the way the pre-fix close path did; return its uuid."""
    before = set((_tracker(repo) / source).glob("*-LINK.json"))
    append_event(
        source,
        "LINK",
        {"target_id": target, "relation": relation},
        _tracker(repo),
        repo_root=repo,
    )
    (new,) = set((_tracker(repo) / source).glob("*-LINK.json")) - before
    return json.loads(new.read_text(encoding="utf-8"))["uuid"]


def _deps(ticket: str, repo: str) -> list[tuple[str, str]]:
    deps = rebar.show_ticket(ticket, repo_root=repo)["deps"]
    return sorted((d["target_id"], d["relation"]) for d in deps)


def _unlink_event_uuids(ticket: str, repo: str) -> list[str]:
    return [
        json.loads(p.read_text(encoding="utf-8"))["data"]["link_uuid"]
        for p in sorted((_tracker(repo) / ticket).glob("*-UNLINK.json"))
    ]


def _compact(ticket: str, repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    rebar.compact(ticket, repo_root=repo)
    tdir = _tracker(repo) / ticket
    assert list(tdir.glob("*-SNAPSHOT.json")), "precondition: compaction wrote a SNAPSHOT"
    assert not list(tdir.glob("*-LINK.json")), "precondition: the LINK was folded in"


@pytest.mark.parametrize("form", ["short", "full"])
def test_stored_unique_quad_is_removed_by_either_form(rebar_repo, form) -> None:
    repo = str(rebar_repo)
    target = rebar.create_ticket("task", "target", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    uuid = _raw_link(repo, bug, target[:4], "caused_by")
    assert _deps(bug, repo) == [(target, "caused_by")], "precondition: show lists the full id"

    rebar.unlink(bug, target[:4] if form == "short" else target, "caused_by", repo_root=repo)

    assert _deps(bug, repo) == []
    assert _unlink_event_uuids(bug, repo) == [uuid]


def test_stored_unique_quad_is_removed_without_a_relation(rebar_repo) -> None:
    repo = str(rebar_repo)
    target = rebar.create_ticket("task", "target", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    _raw_link(repo, bug, target[:4], "caused_by")

    out = rebar.unlink(bug, target, repo_root=repo)

    assert _deps(bug, repo) == []
    assert out is None or out.get("target", target) == target


def test_stored_ambiguous_quad_is_not_removed_by_one_full_id(rebar_repo) -> None:
    repo = str(rebar_repo)
    origin = rebar.create_ticket("task", "the epic", repo_root=repo)
    _make_twin(repo, origin)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    _raw_link(repo, bug, origin[:4], "caused_by")
    before = _deps(bug, repo)

    with pytest.raises(RebarError) as excinfo:
        rebar.unlink(bug, origin, "caused_by", repo_root=repo)

    assert "no LINK event found" in str(excinfo.value)
    assert _deps(bug, repo) == before
    assert _unlink_event_uuids(bug, repo) == []


def test_stored_quad_of_another_ticket_is_never_removed(rebar_repo) -> None:
    repo = str(rebar_repo)
    other = rebar.create_ticket("task", "other", repo_root=repo)
    named = rebar.create_ticket("task", "named", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    _raw_link(repo, bug, other[:4], "caused_by")

    with pytest.raises(RebarError):
        rebar.unlink(bug, named, "caused_by", repo_root=repo)

    assert _deps(bug, repo) == [(other, "caused_by")]
    assert _unlink_event_uuids(bug, repo) == []


def test_only_the_named_tickets_link_is_removed(rebar_repo) -> None:
    repo = str(rebar_repo)
    other = rebar.create_ticket("task", "other", repo_root=repo)
    named = rebar.create_ticket("task", "named", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    _raw_link(repo, bug, other[:4], "caused_by")
    named_uuid = _raw_link(repo, bug, named[:4], "caused_by")

    rebar.unlink(bug, named[:4], "caused_by", repo_root=repo)

    assert _deps(bug, repo) == [(other, "caused_by")]
    assert _unlink_event_uuids(bug, repo) == [named_uuid]


def test_relates_to_short_target_removes_the_reciprocal(rebar_repo) -> None:
    repo = str(rebar_repo)
    peer = rebar.create_ticket("task", "peer", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    _raw_link(repo, bug, peer[:4], "relates_to")
    _raw_link(repo, peer, bug[:4], "relates_to")

    rebar.unlink(bug, peer, "relates_to", repo_root=repo)

    assert _deps(bug, repo) == []
    assert _deps(peer, repo) == []


@pytest.mark.parametrize("form", ["short", "full"])
def test_snapshot_folded_short_target_is_removed_once_it_resolves(
    rebar_repo, monkeypatch: pytest.MonkeyPatch, form
) -> None:
    from rebar._ids import resolve_ticket_id

    repo = str(rebar_repo)
    origin = rebar.create_ticket("task", "the epic", repo_root=repo)
    twin = _make_twin(repo, origin)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    uuid = _raw_link(repo, bug, origin[:4], "caused_by")
    _compact(bug, repo, monkeypatch)  # quad ambiguous now, so the snapshot keeps it raw
    os.rename(_tracker(repo) / twin, _tracker(repo) / f"ffff-{twin[5:]}")
    tracker = str(_tracker(repo))
    assert resolve_ticket_id(origin[:4], tracker, quiet=True) == origin, "precondition"

    rebar.unlink(bug, origin[:4] if form == "short" else origin, "caused_by", repo_root=repo)

    assert _deps(bug, repo) == []
    assert _unlink_event_uuids(bug, repo) == [uuid]


def test_snapshot_folded_dangling_target_is_removed_by_exact_string(
    rebar_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = str(rebar_repo)
    origin = rebar.create_ticket("task", "the epic", repo_root=repo)
    _make_twin(repo, origin)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    uuid = _raw_link(repo, bug, origin[:4], "caused_by")
    _compact(bug, repo, monkeypatch)

    rebar.unlink(bug, origin[:4], "caused_by", repo_root=repo)

    assert _deps(bug, repo) == []
    assert _unlink_event_uuids(bug, repo) == [uuid]
