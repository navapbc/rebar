"""Bug 221b-cbf0-3065-4b96 — inbound create comment bootstrap.

Tests for the fix that fetches pre-existing Jira comments during _apply_inbound_create
and writes COMMENT events for each (excluding loop-breaker-marked comments), normalizing
ADF bodies to text and recording jira_comment_id for dedup on next pass.

RED today (before fix):
  - test_inbound_create_bootstraps_comments: 0 COMMENT events instead of 2
  - test_inbound_create_next_pass_no_double_import: would produce mutations without bootstrap
  - test_inbound_create_get_comments_failure_degrades: no graceful degradation
  - test_inbound_create_adf_body_normalized: ADF body would not be stored
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)
MUTATION_PATH = APPLIER_PATH.parent / "mutation.py"
INBOUND_DIFFER_PATH = APPLIER_PATH.parent / "inbound_differ.py"


def _load_module(canonical_key: str, path: Path):
    if canonical_key in sys.modules:
        return sys.modules[canonical_key]
    spec = importlib.util.spec_from_file_location(canonical_key, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[canonical_key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def mut_mod():
    canonical = "rebar_reconciler.mutation"
    return _load_module(canonical, MUTATION_PATH)


@pytest.fixture(scope="module")
def applier():
    # Load under a unique key to avoid collision with other test modules
    return _load_module("applier_bootstrap_test", APPLIER_PATH)


@pytest.fixture(scope="module")
def inbound_differ():
    canonical = "rebar_reconciler.inbound_differ"
    return _load_module(canonical, INBOUND_DIFFER_PATH)


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    """Isolated tracker directory for each test."""
    monkeypatch.delenv("TICKETS_TRACKER_DIR", raising=False)
    monkeypatch.delenv("REBAR_ENV_ID", raising=False)
    monkeypatch.delenv("REBAR_AUTHOR", raising=False)
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    (tracker / ".env-id").write_text("test-env-id", encoding="utf-8")
    return tmp_path


def _make_inbound_create(mut_mod, *, target, payload):
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target=target,
        payload=payload,
        provenance={"source": "test"},
    )


def _read_events_of_type(tracker_dir: Path, local_id: str, event_type: str) -> list[dict]:
    ticket_dir = tracker_dir / local_id
    events = []
    for path in sorted(ticket_dir.glob("*.json")):
        ev = json.loads(path.read_text())
        if ev.get("event_type") == event_type:
            events.append(ev)
    return events


RECONCILER_MARKER = "<!-- rebar:reconciler-echo -->"


def _plain_comment(cid: str, body: str) -> dict:
    return {"id": cid, "body": body}


def _adf_comment(cid: str, text: str) -> dict:
    return {
        "id": cid,
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": text}]}
            ],
        },
    }


def _marked_comment(cid: str, body: str) -> dict:
    """A comment whose body contains RECONCILER_MARKER (outbound echo)."""
    return {"id": cid, "body": f"{body}\n\n{RECONCILER_MARKER}"}


# ---------------------------------------------------------------------------
# (a) inbound create with 2 plain + 1 marker comment → exactly 2 COMMENT events
# ---------------------------------------------------------------------------


def test_inbound_create_bootstraps_comments(applier, mut_mod, fixture_repo):
    """After inbound create, pre-existing Jira comments are bootstrapped as COMMENT events.

    Scenario: Jira issue DIG-900 has 3 comments:
      - "10001": plain comment → must be bootstrapped
      - "10002": another plain comment → must be bootstrapped
      - "10003": loop-breaker-marked → must be EXCLUDED

    Expected: exactly 2 COMMENT events, with jira_comment_ids "10001" and "10002".
    """
    client = MagicMock()
    client.get_comments.return_value = [
        _plain_comment("10001", "First real comment"),
        _plain_comment("10002", "Second real comment"),
        _marked_comment("10003", "Our own outbound echo"),
    ]

    mutation = _make_inbound_create(
        mut_mod,
        target="DIG-900",
        payload={"summary": "Bootstrap test", "issuetype": "Task"},
    )
    result = applier._apply_typed(mutation, client=client, repo_root=fixture_repo)
    local_id = "jira-dig-900"
    assert result.payload["local_id"] == local_id

    tracker = fixture_repo / ".tickets-tracker"
    comment_events = _read_events_of_type(tracker, local_id, "COMMENT")
    assert len(comment_events) == 2, (
        f"Expected 2 COMMENT events (marker-decorated excluded), "
        f"got {len(comment_events)}: {comment_events!r}"
    )
    jira_ids = {ev["data"]["jira_comment_id"] for ev in comment_events}
    assert jira_ids == {"10001", "10002"}, (
        f"Expected jira_comment_ids {{'10001', '10002'}}, got {jira_ids!r}"
    )
    bodies = {ev["data"]["body"] for ev in comment_events}
    assert "First real comment" in bodies
    assert "Second real comment" in bodies

    # The marker comment must NOT appear
    all_bodies = " ".join(ev["data"]["body"] for ev in comment_events)
    assert RECONCILER_MARKER not in all_bodies, (
        "Loop-breaker-marked comment must not be bootstrapped"
    )


# ---------------------------------------------------------------------------
# (b) next-pass inbound comment diff with same comments → 0 new mutations
# ---------------------------------------------------------------------------


def test_inbound_create_next_pass_no_double_import(applier, mut_mod, inbound_differ, fixture_repo):
    """After bootstrapping, the next inbound comment diff emits 0 mutations.

    Verifies the dedup contract: _diff_comments_inbound keys on jira_comment_id.
    A bootstrapped comment already has jira_comment_id recorded locally, so the
    inbound differ must skip it on subsequent passes.
    """
    client = MagicMock()
    client.get_comments.return_value = [
        _plain_comment("20001", "Already bootstrapped comment"),
        _plain_comment("20002", "Another bootstrapped comment"),
    ]

    mutation = _make_inbound_create(
        mut_mod,
        target="DIG-901",
        payload={"summary": "Dedup test", "issuetype": "Task"},
    )
    applier._apply_typed(mutation, client=client, repo_root=fixture_repo)
    local_id = "jira-dig-901"

    # Verify bootstrap happened (precondition)
    tracker = fixture_repo / ".tickets-tracker"
    comment_events = _read_events_of_type(tracker, local_id, "COMMENT")
    assert len(comment_events) == 2, (
        f"Precondition: expected 2 bootstrapped COMMENT events, got {len(comment_events)}"
    )

    # Simulate next-pass: build the local_ticket as the reducer would surface it,
    # with jira_comment_id fields from the bootstrapped events.
    local_ticket = {
        "comments": [
            {"body": ev["data"]["body"], "jira_comment_id": ev["data"]["jira_comment_id"]}
            for ev in comment_events
        ]
    }

    # jira_fields shape that _diff_comments_inbound consumes
    jira_fields = {
        "comments": [
            _plain_comment("20001", "Already bootstrapped comment"),
            _plain_comment("20002", "Another bootstrapped comment"),
        ]
    }

    mutations = inbound_differ._diff_comments_inbound(jira_fields, local_ticket)
    assert mutations == [], (
        f"Next-pass diff must emit 0 mutations (dedup by jira_comment_id); "
        f"got {mutations!r}"
    )


# ---------------------------------------------------------------------------
# (c) get_comments raises → create succeeds, 0 comment events, warning logged
# ---------------------------------------------------------------------------


def test_inbound_create_get_comments_failure_degrades(applier, mut_mod, fixture_repo, capsys):
    """When get_comments raises, create still succeeds and 0 COMMENT events are written.

    The warning must be emitted to stderr so operators can detect the degradation.
    """
    client = MagicMock()
    client.get_comments.side_effect = RuntimeError("Jira API timeout")

    mutation = _make_inbound_create(
        mut_mod,
        target="DIG-902",
        payload={"summary": "Failure degradation test", "issuetype": "Task"},
    )
    # Must not raise
    result = applier._apply_typed(mutation, client=client, repo_root=fixture_repo)
    local_id = "jira-dig-902"
    assert result.payload["local_id"] == local_id, "create must succeed despite get_comments failure"

    tracker = fixture_repo / ".tickets-tracker"
    # CREATE event must exist
    create_dir = tracker / local_id
    assert create_dir.is_dir(), "ticket directory must be created"
    all_events = sorted(create_dir.glob("*.json"))
    assert any(json.loads(p.read_text()).get("event_type") == "CREATE" for p in all_events), (
        "CREATE event must be written even when get_comments fails"
    )

    # 0 COMMENT events
    comment_events = _read_events_of_type(tracker, local_id, "COMMENT")
    assert comment_events == [], (
        f"No COMMENT events must be written when get_comments fails; "
        f"got {comment_events!r}"
    )

    # Warning must appear on stderr
    captured = capsys.readouterr()
    assert "WARNING" in captured.err or "warning" in captured.err.lower(), (
        f"A warning must be logged to stderr on get_comments failure; "
        f"stderr was: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# (d) ADF-body comment → normalized text stored
# ---------------------------------------------------------------------------


def test_inbound_create_adf_body_normalized(applier, mut_mod, fixture_repo):
    """When a Jira comment body is an ADF dict, it is normalized to plain text.

    The COMMENT event must store the plain-text string, not the raw ADF dict.
    """
    client = MagicMock()
    client.get_comments.return_value = [
        _adf_comment("30001", "ADF paragraph text"),
    ]

    mutation = _make_inbound_create(
        mut_mod,
        target="DIG-903",
        payload={"summary": "ADF normalization test", "issuetype": "Task"},
    )
    applier._apply_typed(mutation, client=client, repo_root=fixture_repo)
    local_id = "jira-dig-903"

    tracker = fixture_repo / ".tickets-tracker"
    comment_events = _read_events_of_type(tracker, local_id, "COMMENT")
    assert len(comment_events) == 1, (
        f"Expected 1 COMMENT event for ADF body, got {len(comment_events)}: {comment_events!r}"
    )
    stored_body = comment_events[0]["data"]["body"]
    assert isinstance(stored_body, str), (
        f"COMMENT body must be a string (ADF normalized), got {type(stored_body)!r}: {stored_body!r}"
    )
    assert "ADF paragraph text" in stored_body, (
        f"Normalized body must contain the ADF text; got {stored_body!r}"
    )
    assert comment_events[0]["data"].get("jira_comment_id") == "30001"
