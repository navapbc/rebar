"""Contract: the CLI ``workflow run|status|result --output json`` payloads conform
to the canonical ``workflow_run`` JSON Schema (epic a88f follow-up).

The MCP status/result tools already advertise + validate ``WORKFLOW_RUN`` via
``outputSchema`` (test_workflow_mcp_schema.py), but the CLI's ``--output json`` was
never pinned to the same schema — so a CLI/MCP shape drift could pass unnoticed.
This closes that: the SAME schema validates all three CLI JSON emissions, over a
real (dry-run, no-token) end-to-end run of the packaged ``code_review`` workflow.
"""

from __future__ import annotations

import json
from pathlib import Path

import rebar
from rebar import schemas
from rebar._cli import main


def _run_cli_json(capsys, argv: list[str]) -> dict:
    rc = main(argv)
    out = capsys.readouterr().out
    payload = json.loads(out)
    return rc, payload


def test_cli_workflow_run_status_result_conform_to_schema(rebar_repo: Path, capsys) -> None:
    validator = schemas.validator(schemas.WORKFLOW_RUN)
    tid = rebar.create_ticket(
        "task",
        "Reviewable",
        description="Do the thing.\n\n## Acceptance Criteria\n- [ ] it works",
        repo_root=str(rebar_repo),
    )

    # `run` — dry-run executes the agent step with the offline FakeRunner (no
    # tokens); --ticket persists run-state so status/result can replay it.
    rc, run_payload = _run_cli_json(
        capsys,
        [
            "workflow",
            "run",
            "code_review",  # resolves from the packaged examples
            "--input",
            f"ticket_id={tid}",
            "--ticket",
            tid,
            "--dry-run",
            "--output",
            "json",
        ],
    )
    validator.validate(run_payload)
    assert run_payload["run_id"]
    assert run_payload["status"] in ("succeeded", "failed", "running")
    run_id = run_payload["run_id"]

    # `status` read — replayed from the ticket's run-state events.
    rc, status_payload = _run_cli_json(
        capsys, ["workflow", "status", run_id, "--ticket", tid, "--output", "json"]
    )
    assert rc == 0
    validator.validate(status_payload)
    assert status_payload["run_id"] == run_id
    assert isinstance(status_payload.get("steps", {}), dict)

    # `result` read — terminal output + per-step outputs.
    rc, result_payload = _run_cli_json(
        capsys, ["workflow", "result", run_id, "--ticket", tid, "--output", "json"]
    )
    assert rc == 0
    validator.validate(result_payload)
    assert result_payload["run_id"] == run_id
