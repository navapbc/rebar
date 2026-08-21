"""Shared runner for the external ``jscpd`` duplication analyzer."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _default_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """The default jscpd runner: a patchable indirection over ``subprocess.run``.

    Deliberately a named function rather than ``run: Runner = subprocess.run`` in
    the signature — a default expression is evaluated ONCE at import, so a frozen
    default silently escapes a test's ``subprocess.run`` patch on its defining
    module and invokes the REAL external ``jscpd`` (bug 9118, same class as
    2c4b/5ea3). Resolves ``subprocess.run`` at CALL time; production behaviour is
    byte-identical, and an explicitly passed ``run=`` bypasses it. Mirrors
    ``access_check._retry_sleep`` / ``_default_client``.
    """
    return subprocess.run(*args, **kwargs)


def run_jscpd(
    scan_root: str | Path,
    *,
    run: Runner = _default_run,
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

    # A ``sources`` count of exactly 0 means jscpd measured NOTHING (an empty or
    # entirely-unsupported scan root) — never "this repository has zero duplication".
    # Reporting it as a zero-valued result would publish a confident structural zero, so
    # this is signalled by raising (the caller converts it to Unavailable) rather than by
    # adding a key to the returned payload, whose shape existing callers assert exactly.
    # The key may be absent on some jscpd versions; only an explicit zero counts.
    sources = total.get("sources")
    if sources == 0 and not isinstance(sources, bool):
        raise ValueError("jscpd report shows zero scanned sources")

    return {"clones": clones, "percentage": percentage}
