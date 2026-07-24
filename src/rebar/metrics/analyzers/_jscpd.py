"""Shared runner for the external ``jscpd`` duplication analyzer."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

Runner = Callable[..., subprocess.CompletedProcess[str]]


def run_jscpd(
    scan_root: str | Path,
    *,
    run: Runner = subprocess.run,
) -> dict[str, int | float]:
    """Run ``jscpd`` and return its total clone count and percentage.

    ``jscpd`` writes its JSON report to the requested output directory rather
    than stdout. The command deliberately resolves ``jscpd`` from ``PATH`` so
    callers share the historical backfill script's invocation behavior.
    """

    with tempfile.TemporaryDirectory() as output_dir:
        command = [
            "jscpd",
            "--reporters",
            "json",
            "--output",
            output_dir,
            str(scan_root),
        ]
        completed = run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise subprocess.SubprocessError(f"jscpd exited with status {completed.returncode}")

        report_path = Path(output_dir) / "jscpd-report.json"
        if not report_path.exists():
            raise ValueError("jscpd did not produce jscpd-report.json")
        report: Any = json.loads(report_path.read_text(encoding="utf-8"))

    total = report["statistics"]["total"]
    clones = total["clones"]
    percentage = total["percentage"]
    if not isinstance(clones, int) or isinstance(clones, bool):
        raise ValueError("jscpd report has invalid total clone count")
    if not isinstance(percentage, int | float) or isinstance(percentage, bool):
        raise ValueError("jscpd report has invalid total clone percentage")
    return {"clones": clones, "percentage": percentage}
