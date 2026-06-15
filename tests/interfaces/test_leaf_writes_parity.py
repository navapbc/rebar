"""Tier B leaf-write behavioral tests — docs/bash-migration.md §4.

Tier B was cut over (default flipped to python) and then retired (switch + bash
leaf bodies deleted) on 2026-06-11, so ``rebar._commands`` is now the sole
leaf-write implementation. These tests assert the in-process write path for every
ported command produces the expected state, read back via ``show_ticket`` — the
characterization that outlived the bash second implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar


def _new_ticket(repo: Path) -> str:
    return rebar.create_ticket("task", "leaf parity ticket", repo_root=str(repo))


def test_library_comment_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    rebar.comment(tid, "a python-path note", repo_root=str(rebar_repo))
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    bodies = [c["body"] for c in state["comments"]]
    assert "a python-path note" in bodies


def test_library_comment_parity_bash_vs_python(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Same comment via bash and python paths → both land, identical body."""
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "bash")
    rebar.comment(tid, "shared note", repo_root=str(rebar_repo))
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    rebar.comment(tid, "shared note", repo_root=str(rebar_repo))
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    bodies = [c["body"] for c in state["comments"]]
    assert bodies.count("shared note") == 2


def test_library_file_impact_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    impact = [{"path": "src/x.py", "reason": "touched"}]
    rebar.set_file_impact(tid, impact, repo_root=str(rebar_repo))
    assert rebar.get_file_impact(tid, repo_root=str(rebar_repo)) == impact


def test_library_verify_commands_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    cmds = [{"dd_id": "DD1", "dd_text": "it builds", "command": "make"}]
    rebar.set_verify_commands(tid, cmds, repo_root=str(rebar_repo))
    assert rebar.get_verify_commands(tid, repo_root=str(rebar_repo)) == cmds


def test_library_python_path_rejects_bad_file_impact(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    with pytest.raises(rebar.RebarError):
        rebar.set_file_impact(tid, [{"path": "x"}], repo_root=str(rebar_repo))


def test_library_tag_roundtrip_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    rebar.tag(tid, "area:api", repo_root=str(rebar_repo))
    assert "area:api" in rebar.show_ticket(tid, repo_root=str(rebar_repo))["tags"]
    rebar.untag(tid, "area:api", repo_root=str(rebar_repo))
    assert "area:api" not in rebar.show_ticket(tid, repo_root=str(rebar_repo))["tags"]


def test_library_tag_idempotent_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    rebar.tag(tid, "dup:tag", repo_root=str(rebar_repo))
    rebar.tag(tid, "dup:tag", repo_root=str(rebar_repo))  # idempotent — no second tag
    tags = rebar.show_ticket(tid, repo_root=str(rebar_repo))["tags"]
    assert tags.count("dup:tag") == 1
    # untag of an absent tag is graceful (no raise)
    rebar.untag(tid, "never:applied", repo_root=str(rebar_repo))


def test_library_archive_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    rebar.archive(tid, repo_root=str(rebar_repo))
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["archived"] is True
    # idempotent: archiving again is a silent no-op
    rebar.archive(tid, repo_root=str(rebar_repo))


def test_library_create_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    res = rebar.create_ticket(
        "story",
        "py-created story",
        priority=1,
        tags=["a", "b"],
        return_alias=True,
        repo_root=str(rebar_repo),
    )
    assert res["id"] and res["alias"]
    state = rebar.show_ticket(res["id"], repo_root=str(rebar_repo))
    assert state["title"] == "py-created story"
    assert state["ticket_type"] == "story"
    assert state["priority"] == 1
    assert set(state["tags"]) == {"a", "b"}


def test_library_create_python_rejects_bad_type(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    with pytest.raises(rebar.RebarError):
        rebar.create_ticket("nonsense", "bad", repo_root=str(rebar_repo))


def test_library_create_python_parent_child(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    epic = rebar.create_ticket("epic", "parent epic", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child task", parent=epic, repo_root=str(rebar_repo))
    assert rebar.show_ticket(child, repo_root=str(rebar_repo))["parent_id"] == epic


def test_library_edit_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    rebar.edit_ticket(tid, title="renamed via python", priority=0, repo_root=str(rebar_repo))
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["title"] == "renamed via python"
    assert state["priority"] == 0


def test_library_edit_python_reparent_and_detach(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    epic = rebar.create_ticket("epic", "edit-parent epic", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "edit-child", repo_root=str(rebar_repo))
    rebar.edit_ticket(child, parent=epic, repo_root=str(rebar_repo))
    assert rebar.show_ticket(child, repo_root=str(rebar_repo))["parent_id"] == epic
    rebar.edit_ticket(child, parent="null", repo_root=str(rebar_repo))  # detach
    assert not rebar.show_ticket(child, repo_root=str(rebar_repo)).get("parent_id")


def test_library_edit_python_rejects_bad_priority_and_empty_title(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    tid = _new_ticket(rebar_repo)
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    with pytest.raises(rebar.RebarError):
        rebar.edit_ticket(tid, priority=99, repo_root=str(rebar_repo))
    with pytest.raises(rebar.RebarError):
        rebar.edit_ticket(tid, title="", repo_root=str(rebar_repo))


def test_library_link_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    a = rebar.create_ticket("task", "link-a", repo_root=str(rebar_repo))
    b = rebar.create_ticket("task", "link-b", repo_root=str(rebar_repo))
    rebar.link(a, b, "relates_to", repo_root=str(rebar_repo))
    deps = rebar.deps(a, repo_root=str(rebar_repo))
    assert any(d.get("target_id") == b for d in deps.get("deps", []))


def test_library_link_python_rejects_bad_relation(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    a = rebar.create_ticket("task", "link-bad-a", repo_root=str(rebar_repo))
    b = rebar.create_ticket("task", "link-bad-b", repo_root=str(rebar_repo))
    with pytest.raises(rebar.RebarError):
        rebar.link(a, b, "not_a_relation", repo_root=str(rebar_repo))


def test_library_unlink_python_roundtrip(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    a = rebar.create_ticket("task", "unlink-a", repo_root=str(rebar_repo))
    b = rebar.create_ticket("task", "unlink-b", repo_root=str(rebar_repo))
    rebar.link(a, b, "relates_to", repo_root=str(rebar_repo))
    assert any(
        d.get("target_id") == b for d in rebar.deps(a, repo_root=str(rebar_repo)).get("deps", [])
    )
    rebar.unlink(a, b, repo_root=str(rebar_repo))
    assert not any(
        d.get("target_id") == b for d in rebar.deps(a, repo_root=str(rebar_repo)).get("deps", [])
    )


def test_library_unlink_python_no_link_errors(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    a = rebar.create_ticket("task", "unlink-x", repo_root=str(rebar_repo))
    b = rebar.create_ticket("task", "unlink-y", repo_root=str(rebar_repo))
    with pytest.raises(rebar.RebarError):  # no LINK between the pair
        rebar.unlink(a, b, repo_root=str(rebar_repo))


def _event_uuid(tracker: Path, tid: str, suffix: str) -> str:
    import json as _json

    for p in (tracker / tid).iterdir():
        if p.name.endswith(suffix) and not p.name.startswith("."):
            return _json.loads(p.read_text())["uuid"]
    raise AssertionError(f"no {suffix} event for {tid}")


def test_revert_core_unarchives_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    from rebar._commands import composer

    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    tid = _new_ticket(rebar_repo)
    rebar.archive(tid, repo_root=str(rebar_repo))
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["archived"] is True
    tracker = Path(rebar_repo) / ".tickets-tracker"
    archived_uuid = _event_uuid(tracker, tid, "-ARCHIVED.json")
    composer.revert_core(tid, archived_uuid, "undo archive", repo_root=str(rebar_repo))
    # reverting the ARCHIVED event clears the marker → un-archived
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["archived"] is False


def test_revert_core_rejects_revert_of_revert(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    from rebar._commands import composer
    from rebar._commands._seam import CommandError

    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    tid = _new_ticket(rebar_repo)
    tracker = Path(rebar_repo) / ".tickets-tracker"
    create_uuid = _event_uuid(tracker, tid, "-CREATE.json")
    composer.revert_core(tid, create_uuid, repo_root=str(rebar_repo))
    revert_uuid = _event_uuid(tracker, tid, "-REVERT.json")
    with pytest.raises(CommandError):
        composer.revert_core(tid, revert_uuid, repo_root=str(rebar_repo))
    with pytest.raises(CommandError):  # unknown uuid
        composer.revert_core(tid, "no-such-uuid", repo_root=str(rebar_repo))


def test_library_archive_status_gate_python_path(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    tid = _new_ticket(rebar_repo)
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))  # open -> in_progress
    monkeypatch.setenv("REBAR_LEAF_WRITES", "python")
    with pytest.raises(rebar.RebarError):  # archive only works on open tickets
        rebar.archive(tid, repo_root=str(rebar_repo))
