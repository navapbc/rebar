"""Happy-path tests for the read-only ``/ticket/<id>`` audit page.

The page renders ``rebar.audit.read.audit_trail`` with HTTP 200, all three gate sections,
and seeded finding content. The held-out companion covers gate-strip counts and anchors,
section order, four-pass values, thresholds, decision grouping, convergence, round selection,
completion, and empty states.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # the [ui] extra; absent in the lean CI suite
pytest.importorskip("httpx")  # starlette TestClient's HTTP backend

import _audit_page_helpers as H

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return H.make_store(tmp_path, monkeypatch)


def _client(repo: str):
    from starlette.testclient import TestClient

    from rebar.audit import server

    return TestClient(server.create_app(repo_root=repo))


def test_ticket_page_renders_and_shows_gate_sections(store: Path) -> None:
    """A ticket with plan-review + completion + code-review audit data renders a
    ``/ticket/<id>`` page (HTTP 200) whose HTML shows all three gate sections and a
    seeded blocking finding's text."""
    import rebar

    r = str(store)
    tid = rebar.create_ticket("task", "audited work", description="x" * 60, repo_root=r)
    H.emit_plan_round(
        r,
        tid,
        findings=[
            H.finding(
                "b1",
                decision="block",
                finding="Unbounded retry budget",
                priority=0.9,
                block_threshold=0.7,
            ),
            H.finding(
                "a1",
                decision="advisory",
                finding="Minor naming nit",
                priority=0.3,
                block_threshold=0.9,
            ),
        ],
        coaching=[
            {
                "move_id": "m1",
                "finding_refs": ["b1"],
                "move_name": "cap",
                "subject": "retry",
                "coaching": "add a ceiling",
            }
        ],
    )
    H.emit_completion_pass(
        r,
        tid,
        [
            {
                "criterion": "AC1 works",
                "met": True,
                "kind": "codebase-verifiable",
                "citation": "src/x.py:1",
            }
        ],
    )
    H.emit_code_review(r, tid, advisory=[{"finding": "code nit", "location": "src/y.py:2"}])

    resp = _client(r).get(f"/ticket/{tid}")
    assert resp.status_code == 200
    body = resp.text.lower()
    # all three gate sections present
    assert "plan" in body and "completion" in body and "code" in body
    # the seeded blocking finding's text is rendered
    assert "Unbounded retry budget" in resp.text
