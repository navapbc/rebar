"""Held-out regression for closing with a tracker outside the code repository."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import rebar
import rebar.llm
from rebar import config
from rebar._commands import transition as transition_command


def test_cli_close_uses_code_repo_when_tracker_is_relocated(
    rebar_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    (rebar_repo / "rebar.toml").write_text(
        "[verify]\nrequire_completion_verification_for_close = true\n"
    )
    external_tracker = tmp_path / "external-store" / "tickets"
    external_tracker.parent.mkdir()
    shutil.move(str(rebar_repo / ".tickets-tracker"), external_tracker)
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(external_tracker))
    monkeypatch.chdir(rebar_repo)
    config.reset_config_cache()

    calls: list[str] = []

    def _pass(ticket_id: str, **kwargs: object) -> dict[str, object]:
        calls.append(ticket_id)
        return {"verdict": "PASS", "findings": [], "runner": "fake", "model": "fake"}

    monkeypatch.setattr(rebar.llm, "verify_completion", _pass)
    description = "Body.\n\n## Acceptance Criteria\n- [x] done\n\n## Context\ncontext\n"
    ticket_id = rebar.create_ticket("task", "relocated tracker", description=description)
    rebar.transition(ticket_id, "open", "in_progress")
    rebar.set_file_impact(ticket_id, [{"path": "src/x.py", "reason": "touched"}])
    subprocess.run(
        [
            "git",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            f"work\n\nrebar-ticket: {ticket_id}",
        ],
        cwd=rebar_repo,
        check=True,
    )

    assert transition_command.transition_cli([ticket_id, "in_progress", "closed"]) == 0
    assert calls == [ticket_id]
    assert rebar.show_ticket(ticket_id)["status"] == "closed"
    assert rebar.verify_signature(ticket_id)["verdict"] == "certified"
