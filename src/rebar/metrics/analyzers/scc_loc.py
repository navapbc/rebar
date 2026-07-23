"""Line-of-code analysis backed by the external ``scc`` command."""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.registry import Unavailable

_ACCRUING_SINCE = "2026-01-01T00:00:00+00:00"
_LOGGER = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run_scc(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run an scc command with text output captured for parsing."""

    return subprocess.run(command, capture_output=True, check=False, text=True, **kwargs)


def analyze(
    repo_root: Path,
    scan_roots: Iterable[str | Path] | None = None,
    *,
    run: Runner = _run_scc,
) -> AnalyzerResult | Unavailable:
    """Return per-file LOC from scc, or an unavailable metric on tool failure."""

    root = repo_root.resolve()
    roots = _scan_roots(root, scan_roots)
    files: dict[str, int] = {}

    for scan_root in roots:
        command = ["scc", "--format", "json", str(scan_root)]
        try:
            completed = run(command)
        except OSError as exc:
            return _unavailable(f"could not run scc: {exc}")

        if completed.returncode != 0:
            return _unavailable(f"scc exited with status {completed.returncode}")
        if not completed.stdout.strip():
            return _unavailable("scc produced no output")

        try:
            payload = json.loads(completed.stdout)
            files.update(_parse_files(payload, root))
        except (TypeError, ValueError) as exc:
            return _unavailable(f"scc produced invalid JSON: {exc}")

    return AnalyzerResult(loc={"files": files, "max_loc": max(files.values(), default=0)})


def _scan_roots(repo_root: Path, scan_roots: Iterable[str | Path] | None) -> list[Path]:
    """Choose unique scan roots in a stable order."""

    candidates = [] if scan_roots is None else list(scan_roots)
    if not candidates:
        candidates = [repo_root]
    roots = set()
    for candidate in candidates:
        path = Path(candidate)
        roots.add((path if path.is_absolute() else repo_root / path).resolve())
    return sorted(roots, key=lambda candidate: candidate.as_posix())


def _parse_files(payload: object, repo_root: Path) -> dict[str, int]:
    """Flatten scc's language-grouped JSON into repo-relative file LOC."""

    if not isinstance(payload, list):
        raise ValueError("top-level value is not a list")

    files: dict[str, int] = {}
    for language in payload:
        if not isinstance(language, dict):
            raise ValueError("language group is not an object")
        entries = language.get("Files")
        if not isinstance(entries, list):
            raise ValueError("language group has no file list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("file entry is not an object")
            location = entry.get("Location")
            code = entry.get("Code")
            if not isinstance(location, str) or not isinstance(code, int):
                raise ValueError("file entry has invalid location or code")
            files[_relative_location(location, repo_root)] = code
    return files


def _relative_location(location: str, repo_root: Path) -> str:
    """Normalize an scc file location relative to the repository root."""

    path = Path(location)
    if path.is_absolute():
        try:
            path = path.relative_to(repo_root)
        except ValueError as exc:
            raise ValueError(f"file location outside repository: {location}") from exc
    return path.as_posix()


def _unavailable(reason: str) -> Unavailable:
    """Log and build the standard unavailable result."""

    _LOGGER.warning("scc unavailable: %s", reason)
    return Unavailable(reason=reason, accruing_since=_ACCRUING_SINCE)
