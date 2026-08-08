"""Held-out production-workflow oracle for the additive bridge vocabulary migration."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _ROOT / ".github" / "workflows" / "reconcile-bridge.yml"


def _run_step() -> str:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    matches = [
        step["run"]
        for step in workflow["jobs"]["reconcile"]["steps"]
        if step.get("name") == "Run reconciler"
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize(
    ("mode", "expected_argv"),
    [
        ("dry-run", ["bridge", "preview"]),
        ("bootstrap-strict", ["bridge", "sync", "--max-changes", "10"]),
        ("bootstrap-throttle", ["bridge", "sync", "--max-changes", "100"]),
        ("live", ["bridge", "sync"]),
    ],
)
def test_workflow_maps_every_rollback_setting_to_the_continuous_command(
    tmp_path: Path, mode: str, expected_argv: list[str]
) -> None:
    """The shipped shell block selects the exact primary or diagnostic route."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_file = tmp_path / "argv"
    output_file = tmp_path / "github-output"
    output_file.write_text("", encoding="utf-8")
    stub = bin_dir / "rebar"
    stub.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$ARGV_FILE"\nexit 0\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "MODE": mode,
            "ARGV_FILE": str(argv_file),
            "GITHUB_OUTPUT": str(output_file),
        }
    )

    completed = subprocess.run(
        ["bash", "-c", _run_step()],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert argv_file.read_text(encoding="utf-8").splitlines() == expected_argv


def test_current_operator_docs_lead_with_bridge_and_retain_legacy_mapping() -> None:
    """Primary instructions migrate while the compatibility contract stays discoverable."""
    workflow_step = _run_step()
    assert "reconcile-check" in workflow_step
    assert "rebar reconcile --mode reconcile-check" in workflow_step

    combined = "\n".join(
        (_ROOT / path).read_text(encoding="utf-8")
        for path in (
            "docs/user-guide.md",
            "docs/cli-reference.md",
            "docs/jira-sync-setup.md",
            "docs/release-notes.md",
        )
    )

    assert "rebar bridge preview" in combined
    assert "rebar bridge sync" in combined
    assert "rebar reconcile" in combined
    normalized = " ".join(combined.split())
    assert "no arguments still mean dry-run" in normalized
    assert "python -m rebar_reconciler" in combined
    assert "argument-less direct engine invocation stays live" in normalized
