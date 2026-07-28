"""Held-out edge and store-boundary oracles for the eval fixture heading collapse."""

from __future__ import annotations

import pytest

import rebar
from rebar.llm.evals import eval_solver
from rebar.llm.runner import FakeRunner


def test_spec_alignment_fixtures_keep_payloads_under_one_heading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_descriptions: list[str] = []
    create_ticket = rebar.create_ticket

    def capture_create_ticket(*args: object, **kwargs: object) -> object:
        if args and args[0] == "epic":
            captured_descriptions.append(str(kwargs["description"]))
        return create_ticket(*args, **kwargs)

    monkeypatch.setattr(rebar, "create_ticket", capture_create_ticket)
    payloads = [
        "Epic A: event ingestion",
        "Epic B: preserve generic success criteria wording in source material",
    ]

    eval_solver.run_case(
        "spec-alignment",
        {
            "id": "sa-multiple-headings",
            "expect": "pass",
            "spec": "MUST ingest events.",
            "epics": payloads,
        },
        runner=FakeRunner(findings=[]),
    )

    assert len(captured_descriptions) == len(payloads)
    for description, payload in zip(captured_descriptions, payloads, strict=True):
        assert description.count("## Acceptance Criteria") == 1
        assert "## Success Criteria" not in description
        assert payload in description


def test_spec_alignment_persists_the_single_heading_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted_descriptions: list[str] = []
    create_ticket = rebar.create_ticket

    def capture_persisted_ticket(*args: object, **kwargs: object) -> object:
        created = create_ticket(*args, **kwargs)
        if args and args[0] == "epic":
            ticket = rebar.show_ticket(str(created["id"]), repo_root=kwargs["repo_root"])
            persisted_descriptions.append(str(ticket["description"]))
        return created

    monkeypatch.setattr(rebar, "create_ticket", capture_persisted_ticket)

    eval_solver.run_case(
        "spec-alignment",
        {
            "id": "sa-persisted-heading",
            "expect": "pass",
            "spec": "MUST ingest events.",
            "epics": ["Epic A: event ingestion"],
        },
        runner=FakeRunner(findings=[]),
    )

    assert len(persisted_descriptions) == 1
    assert persisted_descriptions[0].count("## Acceptance Criteria") == 1
    assert "## Success Criteria" not in persisted_descriptions[0]
