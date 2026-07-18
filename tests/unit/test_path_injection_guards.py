"""Path-containment guards — regression cover for the remaining CodeQL
``py/path-injection`` + ``py/stack-trace-exposure`` alerts (bug
illbehaved-girlish-bubblefish).

Each test pins one containment barrier the fix introduced:

* :func:`rebar._ids.resolve_ticket_id` / :func:`resolve_ticket_dir_name` refuse a
  traversing / absolute id and never hand back a raw path segment;
* the plan-review sidecar reader no longer falls back to the raw id (``... or
  ticket_id``), so a traversal id reads nothing instead of escaping the tracker;
* the workflow editor's ``_raw_prompt_text`` cannot read a file outside the
  project prompt dir;
* the review-bot ``/rerun`` 502 body carries no exception/stack-trace detail.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

from rebar._ids import resolve_ticket_dir_name, resolve_ticket_id

_VALID = "abcd-1234-ef56-7890"


def _tracker_with_ticket(tmp_path: Path) -> str:
    tracker = tmp_path / "tracker"
    (tracker / _VALID).mkdir(parents=True)
    # A real directory OUTSIDE the tracker that a `..` traversal would try to reach.
    (tmp_path / "secret").mkdir()
    return str(tracker)


# ── resolve_ticket_id: accepts valid forms, rejects traversal ────────────────


def test_resolve_ticket_id_accepts_valid_forms(tmp_path: Path) -> None:
    tracker = _tracker_with_ticket(tmp_path)
    assert resolve_ticket_id(_VALID, tracker) == _VALID  # exact / fast path
    assert resolve_ticket_id("abcd-1234", tracker) == _VALID  # 8-hex short prefix
    assert resolve_ticket_id("abcd", tracker) == _VALID  # >=4-char unique prefix


@pytest.mark.parametrize(
    "hostile",
    [
        "../secret",
        "../../etc/passwd",
        "..",
        ".",
        "/etc/passwd",
        f"{_VALID}/../../secret",
        ".hidden",
        "a/b",
        "a\\b",
        "",
    ],
)
def test_resolve_ticket_id_rejects_unsafe_segments(tmp_path: Path, hostile: str) -> None:
    tracker = _tracker_with_ticket(tmp_path)
    assert resolve_ticket_id(hostile, tracker) is None


def test_resolve_ticket_id_refuses_real_sibling_via_dotdot(tmp_path: Path) -> None:
    # `<tracker>/../secret` names a real directory, but it escapes the tracker and
    # must not resolve (the fast path used to return the raw input verbatim here).
    tracker = _tracker_with_ticket(tmp_path)
    assert resolve_ticket_id("../secret", tracker) is None


# ── resolve_ticket_dir_name: basename on success, FileNotFoundError otherwise ─


def test_resolve_ticket_dir_name_returns_bare_segment(tmp_path: Path) -> None:
    tracker = _tracker_with_ticket(tmp_path)
    name = resolve_ticket_dir_name(_VALID, tracker)
    assert name == _VALID
    assert "/" not in name and "\\" not in name and ".." not in name


@pytest.mark.parametrize("hostile", ["../secret", "..", "/etc/passwd", "nope-nope-nope-nope"])
def test_resolve_ticket_dir_name_raises_on_unresolvable(tmp_path: Path, hostile: str) -> None:
    tracker = _tracker_with_ticket(tmp_path)
    with pytest.raises(FileNotFoundError):
        resolve_ticket_dir_name(hostile, tracker)


# ── plan-review sidecar reader: no raw-id fallback → no tracker escape ────────


def test_plan_review_reader_refuses_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar import config as _config
    from rebar.llm.plan_review import sidecar

    tracker = tmp_path / "tracker"
    (tracker / _VALID).mkdir(parents=True)
    (tracker / _VALID / "00000000000000001-u-REVIEW_RESULT.json").write_text(
        json.dumps({"data": {"schema": "plan_review_result_v2", "findings": []}}),
        encoding="utf-8",
    )
    # A same-named sidecar OUTSIDE the tracker that a `..` traversal would reach.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "00000000000000001-u-REVIEW_RESULT.json").write_text(
        json.dumps({"data": {"schema": "plan_review_result_v2", "findings": ["LEAK"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(_config, "tracker_dir", lambda repo_root=None: str(tracker))

    # Valid id still resolves to the in-tracker record …
    got = sidecar.latest_review_result(_VALID)
    assert got is not None and got["findings"] == []
    # … a traversal id resolves to nothing — the outside "LEAK" record is never read.
    assert sidecar.latest_review_result("../outside") is None
    assert sidecar.all_review_results("../outside") == []


# ── editor _raw_prompt_text: contained to the project prompt dir ──────────────


def test_editor_raw_prompt_text_refuses_traversal(tmp_path: Path) -> None:
    from rebar.llm.workflow.editor_server import _Handler

    prompts = tmp_path / "repo" / ".rebar" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "real.md").write_text("REAL", encoding="utf-8")
    # A readable file that a `../` traversal from the prompts dir would land on.
    (tmp_path / "repo" / ".rebar" / "secret.md").write_text("SECRET", encoding="utf-8")

    handler = _Handler.__new__(_Handler)
    handler.session = types.SimpleNamespace(repo_root=str(tmp_path / "repo"))

    assert handler._raw_prompt_text("real") == "REAL"
    assert handler._raw_prompt_text("../secret") is None  # reachable file, but refused
    assert handler._raw_prompt_text("../../etc/passwd") is None


# ── review-bot /rerun: 502 body has no exception detail ──────────────────────


def test_rerun_502_body_has_no_exception_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastapi")  # the reviewbot extra; absent in the lean CI suite
    from rebar.review_bot import gerrit_client
    from rebar.review_bot.app import rerun
    from rebar.review_bot.gerrit_client import GerritError

    class _FakeGerrit:
        def __init__(self, cfg):  # noqa: D107
            pass

        def get_change_event(self, change):
            raise GerritError("SEKRIT internal detail: /etc/passwd stacktrace", status=500)

    monkeypatch.setattr(gerrit_client, "GerritClient", _FakeGerrit)

    cfg = types.SimpleNamespace(webhook_token="tok")
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(config=cfg)),
        query_params={"token": "tok", "change": "12345"},
    )
    resp = asyncio.run(rerun(req))

    assert resp.status_code == 502
    assert json.loads(bytes(resp.body)) == {"status": "gerrit error"}
    assert b"SEKRIT" not in resp.body and b"stacktrace" not in resp.body
