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


# raw-git-ok: generic command runner, argv supplied by caller
def _run_scc(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run an scc command with text output captured for parsing."""

    return subprocess.run(command, capture_output=True, check=False, text=True, **kwargs)


def analyze(
    repo_root: Path,
    scan_roots: Iterable[str | Path] | None = None,
    *,
    include_extensions: Iterable[str] | None = None,
    run: Runner = _run_scc,
) -> AnalyzerResult | Unavailable:
    """Return per-file LOC from scc, or an unavailable metric on tool failure."""

    root = repo_root.resolve()
    roots = _scan_roots(root, scan_roots)
    extensions = [str(extension) for extension in (include_extensions or ())]
    files: dict[str, int] = {}

    for scan_root in roots:
        command = _scc_command(scan_root, extensions)
        try:
            completed = run(command)
        except FileNotFoundError:
            return _unavailable("scc executable is unavailable")
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

    # An empty map means scc measured NOTHING (a bad filter, an empty root, or a build that
    # emitted only language summaries) — never "this repository has zero lines". Reporting it
    # as a zero-valued result would publish a confident structural zero, so it is unavailable.
    if not files:
        return _unavailable("scc reported no files")

    return AnalyzerResult(loc={"files": files, "max_loc": max(files.values(), default=0)})


def _scc_command(scan_root: Path, extensions: list[str]) -> list[str]:
    """Build the scc argv for one scan root, narrowing by extension when configured."""

    # ``--by-file`` is REQUIRED: without it scc emits per-language summaries whose ``Files``
    # key is present but empty, which parses cleanly into zero entries.
    command = ["scc", "--by-file"]
    if extensions:
        command += ["--include-ext", ",".join(extensions)]
    return [*command, "--format", "json", str(scan_root)]


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
            # ``Lines`` (raw line count), not ``Code``: the CI module-size gate measures
            # `wc -l`, which scc's ``Lines`` matches exactly while ``Code`` never can.
            lines = entry.get("Lines")
            if not isinstance(location, str) or not isinstance(lines, int):
                raise ValueError("file entry has invalid location or line count")
            files[_relative_location(location, repo_root)] = lines
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
