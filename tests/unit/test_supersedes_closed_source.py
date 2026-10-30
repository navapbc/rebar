"""A closed replacement can record ``supersedes``, so a superseded ticket can close honestly.

The disposition reader (``close_disposition.replacement_of``) accepts any replacement that
``close_precheck._is_live_ticket`` accepts, closed ones included. The link writer must agree
for ``supersedes``. Every other relation keeps refusing a closed source; ``blocks`` and
``depends_on`` are pinned here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import close_disposition, close_precheck, gates, transition
from rebar._commands.link_revert import link_cli
from rebar.reducer import reduce_ticket

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.chdir(repo)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: Path) -> str:
    return str(repo / ".tickets-tracker")


def _state(repo: Path, tid: str) -> dict:
    return reduce_ticket(str(repo / ".tickets-tracker" / tid)) or {}


def _decomposed(repo: Path) -> tuple[str, str]:
    """Epic E holds an open story S and a sibling slice T that absorbed S and is closed."""
    epic = rebar.create_ticket("epic", "E", repo_root=str(repo))
    story = rebar.create_ticket("story", "S: absorbed by its slices", repo_root=str(repo))
    rebar.edit_ticket(story, parent=epic, repo_root=str(repo))
    slice_ = rebar.create_ticket("task", "T: the slice that did S's work", repo_root=str(repo))
    rebar.edit_ticket(slice_, parent=epic, repo_root=str(repo))
    rebar.transition(slice_, "open", "closed", repo_root=str(repo))
    return story, slice_


def _supersedes_targets(repo: Path, tid: str) -> set[str]:
    return {
        str(dep.get("target_id"))
        for dep in _state(repo, tid).get("deps") or []
        if dep.get("relation") == "supersedes"
    }


# --- AC1: a closed source can record supersedes ---------------------------------------------


def test_library_writes_supersedes_from_a_closed_source(store):
    story, slice_ = _decomposed(store)

    rebar.link(slice_, story, "supersedes", repo_root=str(store))

    assert _supersedes_targets(store, slice_) == {story}


def test_cli_writes_supersedes_from_a_closed_source(store):
    story, slice_ = _decomposed(store)

    assert link_cli([slice_, story, "supersedes"]) == 0
    assert _supersedes_targets(store, slice_) == {story}


# --- AC2: the guard is narrowed, not removed ------------------------------------------------


@pytest.mark.parametrize("relation", ["blocks", "depends_on"])
def test_blocking_relations_still_refuse_a_closed_source(store, relation):
    story, slice_ = _decomposed(store)

    with pytest.raises(Exception) as caught:
        rebar.link(slice_, story, relation, repo_root=str(store))

    message = str(caught.value)
    assert f"cannot create {relation} link" in message
    assert f"source ticket '{slice_}' is closed" in message
    assert not any(
        dep.get("relation") == relation for dep in _state(store, slice_).get("deps") or []
    )


# --- AC3: the superseded story closes through both armed gates, without force ----------------


def _arm_close_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    armed = {"require_plan_review_for_close", "require_completion_verification_for_close"}
    monkeypatch.setattr(gates, "gate_enabled", lambda _root, name, **_k: name in armed)


def _forbid_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    import rebar.llm as _llm

    def _fail(*_a, **_k):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("the billable completion verifier must not run")

    monkeypatch.setattr(_llm, "verify_completion", _fail)


def test_superseded_story_closes_with_a_signed_disposition(store, monkeypatch):
    story, slice_ = _decomposed(store)
    rebar.link(slice_, story, "supersedes", repo_root=str(store))
    _arm_close_gates(monkeypatch)
    _forbid_verifier(monkeypatch)

    check = gates.close_plan_review_gate_check(
        story,
        _state(store, story),
        repo_root=str(store),
        close_class="superseded",
        tracker=_tracker(store),
    )
    assert check["ok"] is True
    assert check["verdict"] == "disposition"

    assert transition.transition_cli([story, "open", "closed", "--class=superseded"]) == 0

    state = _state(store, story)
    assert state["status"] == "closed"
    assert state["close_class"] == "superseded"
    assert "force_close_reason" not in state
    manifest = ((state.get("attestations") or {}).get("completion-verifier") or {}).get(
        "manifest"
    ) or []
    assert manifest and manifest[0] == "completion-verifier: DISPOSITION superseded"
    assert f"replacement: {slice_}" in manifest


def test_superseded_close_without_the_link_is_still_refused(store, monkeypatch):
    story, _slice = _decomposed(store)
    _arm_close_gates(monkeypatch)
    _forbid_verifier(monkeypatch)

    check = gates.close_plan_review_gate_check(
        story,
        _state(store, story),
        repo_root=str(store),
        close_class="superseded",
        tracker=_tracker(store),
    )

    assert check["ok"] is False
    assert check["verdict"] != "disposition"


# --- AC4: the writer creates every source the reader accepts --------------------------------


@pytest.mark.parametrize("source_status", ["open", "in_progress", "closed"])
def test_writer_and_reader_agree_on_usable_sources(store, source_status):
    story = rebar.create_ticket("story", "S", repo_root=str(store))
    source = rebar.create_ticket("task", f"replacement ({source_status})", repo_root=str(store))
    if source_status == "in_progress":
        rebar.transition(source, "open", "in_progress", repo_root=str(store))
    elif source_status == "closed":
        rebar.transition(source, "open", "closed", repo_root=str(store))
    assert _state(store, source)["status"] == source_status

    reader_accepts = close_precheck._is_live_ticket(source, _tracker(store))
    assert reader_accepts, f"the reader must accept a {source_status} replacement"

    rebar.link(source, story, "supersedes", repo_root=str(store))

    assert _supersedes_targets(store, source) == {story}
    assert close_disposition.replacement_of(story, _tracker(store), require_live=True) == source
