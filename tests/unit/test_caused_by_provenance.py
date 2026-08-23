"""Provenance marker on ``caused_by`` LINK events (ticket 6536-367c).

Measurement 50e3-6d2a found the recorded ``caused_by`` edges could not be partitioned
into blame-derived guesses vs explicitly supplied attributions — both origins wrote
through ``_write_link_event`` with no provenance field, so on read a guessed edge was
indistinguishable from a proven one and the escaped-defect lenses weighted them
identically.

Pinned here:

* a bug close with an EMPTY ``--caused-by`` whose edge comes from blame auto-derivation
  marks the LINK event ``provenance="derived"``;
* a close with an explicit ``--caused-by`` marks ``provenance="explicit"``;
* ``rebar link <bug> <origin> caused_by`` marks ``provenance="explicit"`` (a linked
  attribution is supplied by the caller, never blame-derived);
* other relations carry NO provenance field — the marker is caused_by-scoped;
* replay of a pre-marker LINK event (no field) reduces to a dep entry WITHOUT a
  provenance key: the legacy cohort reads as unknown, shape unchanged.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

import rebar

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


def _caused_by_deps(tid: str, repo: str) -> list[dict]:
    deps = rebar.show_ticket(tid, repo_root=repo)["deps"]
    return [d for d in deps if d["relation"] == "caused_by"]


def _stub_blame(monkeypatch: pytest.MonkeyPatch, culprit: str | None) -> None:
    from rebar.metrics import blame

    monkeypatch.setattr(blame, "derive_caused_by", lambda *_a, **_k: culprit)


def test_blame_derived_close_marks_derived(rebar_repo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = str(rebar_repo)
    origin = rebar.create_ticket("task", "the origin change", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)
    _stub_blame(monkeypatch, origin)

    rebar.transition(bug, "in_progress", "closed", close_class="regression", repo_root=repo)

    deps = _caused_by_deps(bug, repo)
    assert [d["target_id"] for d in deps] == [origin]
    assert [d.get("provenance") for d in deps] == ["derived"]


def test_explicit_flag_close_marks_explicit(rebar_repo) -> None:
    repo = str(rebar_repo)
    origin = rebar.create_ticket("task", "the origin change", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)
    rebar.transition(bug, "open", "in_progress", repo_root=repo)

    rebar.transition(
        bug, "in_progress", "closed", close_class="regression", caused_by=origin, repo_root=repo
    )

    deps = _caused_by_deps(bug, repo)
    assert [d["target_id"] for d in deps] == [origin]
    assert [d.get("provenance") for d in deps] == ["explicit"]


def test_link_command_marks_explicit(rebar_repo) -> None:
    repo = str(rebar_repo)
    origin = rebar.create_ticket("task", "the origin change", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)

    rebar.link(bug, origin, "caused_by", repo_root=repo)

    deps = _caused_by_deps(bug, repo)
    assert [d.get("provenance") for d in deps] == ["explicit"]


def test_non_caused_by_link_carries_no_marker(rebar_repo) -> None:
    repo = str(rebar_repo)
    a = rebar.create_ticket("task", "a", repo_root=repo)
    b = rebar.create_ticket("task", "b", repo_root=repo)

    rebar.link(a, b, "relates_to", repo_root=repo)

    deps = rebar.show_ticket(a, repo_root=repo)["deps"]
    assert deps, "the relates_to link must be recorded"
    assert all("provenance" not in d for d in deps), (
        "the provenance marker is caused_by-scoped; other relations keep their shape"
    )


def test_legacy_unmarked_event_replays_without_provenance_key(rebar_repo) -> None:
    """A pre-marker LINK event (no field) reduces to the exact prior dep shape."""
    repo = str(rebar_repo)
    origin = rebar.create_ticket("task", "the origin change", repo_root=repo)
    bug = rebar.create_ticket("bug", "the bug", repo_root=repo)

    from rebar.config import tracker_dir

    bug_dir = Path(str(tracker_dir(repo))) / bug
    ts = 1_700_000_000_000_000_000
    ev_uuid = str(uuid.uuid4())
    event = {
        "event_type": "LINK",
        "timestamp": ts,
        "uuid": ev_uuid,
        "env_id": "eeee-0000-4000-8000-000000000001",
        "author": "legacy",
        "data": {"target_id": origin, "relation": "caused_by"},
    }
    (bug_dir / f"{ts:020d}-{ev_uuid}-LINK.json").write_text(json.dumps(event), encoding="utf-8")

    deps = _caused_by_deps(bug, repo)
    assert [d["target_id"] for d in deps] == [origin]
    assert all("provenance" not in d for d in deps), (
        "an unmarked legacy event must read as unknown — no fabricated provenance key"
    )
