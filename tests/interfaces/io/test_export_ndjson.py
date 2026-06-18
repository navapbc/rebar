"""Export NDJSON over a real store (P1.2 T2): schema validity, scope, strip, streaming.

Uses the ``rebar_repo`` fixture (an initialized rebar repo in a temp git dir) from
tests/interfaces/conftest.py.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import rebar
from rebar import schemas

jsonschema = pytest.importorskip("jsonschema")
pytest.importorskip("referencing")


def _export_lines(repo: Path, **kw) -> list[dict]:
    buf = io.StringIO()
    rebar.export_tickets(out=buf, repo_root=str(repo), **kw)
    return [json.loads(ln) for ln in buf.getvalue().splitlines()]


def _seed(repo: Path) -> dict:
    root = str(repo)
    epic = rebar.create_ticket("epic", "Epic", description="d" * 60, repo_root=root)
    task = rebar.create_ticket("task", "Task", repo_root=root)
    rebar.comment(task, "note", repo_root=root)
    rebar.transition(task, "open", "in_progress", repo_root=root)
    closed = rebar.create_ticket("task", "Done", repo_root=root)
    rebar.transition(closed, "open", "closed", repo_root=root)
    log = rebar.create_ticket("session_log", "Log", repo_root=root)
    return {"epic": epic, "task": task, "closed": closed, "log": log}


def test_every_line_validates_against_export_schema(rebar_repo: Path) -> None:
    _seed(rebar_repo)
    validator = schemas.validator(schemas.EXPORT)
    lines = _export_lines(rebar_repo)
    assert lines, "expected at least one exported ticket"
    for line in lines:
        validator.validate(line)
        assert line["schema_version"] == rebar._io.EXPORT_SCHEMA_VERSION


def test_scope_defaults_exclude_session_logs_include_closed(rebar_repo: Path) -> None:
    ids = _seed(rebar_repo)
    lines = _export_lines(rebar_repo)
    by_id = {ln["ticket_id"] for ln in lines}
    assert ids["log"] not in by_id, "session_log excluded by default"
    assert ids["closed"] in by_id, "closed tickets are exported by default"
    types = {ln["ticket_type"] for ln in lines}
    assert "session_log" not in types


def test_include_session_logs_opt_in(rebar_repo: Path) -> None:
    ids = _seed(rebar_repo)
    lines = _export_lines(rebar_repo, include_session_logs=True)
    assert ids["log"] in {ln["ticket_id"] for ln in lines}


def test_filters_status_type_parent(rebar_repo: Path) -> None:
    ids = _seed(rebar_repo)
    # status filter
    only_closed = _export_lines(rebar_repo, status="closed")
    assert {ln["ticket_id"] for ln in only_closed} == {ids["closed"]}
    # type filter
    only_epic = _export_lines(rebar_repo, ticket_type="epic")
    assert {ln["ticket_id"] for ln in only_epic} == {ids["epic"]}
    # parent filter (re-parent the task under the epic first)
    rebar.edit_ticket(ids["task"], parent=ids["epic"], repo_root=str(rebar_repo))
    children = _export_lines(rebar_repo, parent=ids["epic"])
    assert {ln["ticket_id"] for ln in children} == {ids["task"]}


def test_strip_external_removes_bridge_and_jira_comment_id(rebar_repo: Path) -> None:
    ids = _seed(rebar_repo)
    # A comment carrying a jira_comment_id (inbound-from-Jira shape).
    from rebar import config
    from rebar._commands._seam import append_event

    tracker = config.tracker_dir(str(rebar_repo))
    append_event(
        ids["task"],
        "COMMENT",
        {"body": "from jira", "jira_comment_id": "10001"},
        tracker,
        repo_root=str(rebar_repo),
    )
    # Without strip: the jira_comment_id is present.
    plain = _export_lines(rebar_repo)
    task_line = next(ln for ln in plain if ln["ticket_id"] == ids["task"])
    assert any("jira_comment_id" in c for c in task_line["comments"])
    # With strip: no external linkage anywhere.
    stripped = _export_lines(rebar_repo, strip_external=True)
    for ln in stripped:
        assert "bridge_alerts" not in ln
        for c in ln.get("comments", []):
            assert "jira_comment_id" not in c


def test_writes_to_file_and_returns_metadata(rebar_repo: Path, tmp_path: Path) -> None:
    _seed(rebar_repo)
    out = tmp_path / "dump.ndjson"
    meta = rebar.export_tickets(out=str(out), repo_root=str(rebar_repo))
    assert out.exists()
    n_lines = len(out.read_text().splitlines())
    assert meta["exported"] == n_lines
    assert meta["schema_version"] == rebar._io.EXPORT_SCHEMA_VERSION
    assert meta["source_env"]


def test_streams_without_materializing_all_states(rebar_repo: Path, monkeypatch) -> None:
    """Export must NOT route through reduce_all_tickets (the memory-heavy path).

    Sabotage reduce_all_tickets so any reliance on it would raise; a streaming
    export (per-ticket reduce_ticket) is unaffected. Also assert the public
    iterator is lazy (a generator), so memory stays flat regardless of store size.
    """
    _seed(rebar_repo)

    import rebar.reducer as _reducer

    def _boom(*_a, **_k):  # pragma: no cover - must never be called
        raise AssertionError("export must stream via reduce_ticket, not reduce_all_tickets")

    monkeypatch.setattr(_reducer, "reduce_all_tickets", _boom)

    from rebar import config
    from rebar._io.export_ndjson import iter_export_states

    gen = iter_export_states(tracker=str(config.tracker_dir(str(rebar_repo))))
    import types

    assert isinstance(gen, types.GeneratorType)
    assert len(list(gen)) >= 1
