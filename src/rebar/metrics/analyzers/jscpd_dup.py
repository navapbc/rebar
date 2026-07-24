"""Duplication analysis backed by the external ``jscpd`` command."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.analyzers._jscpd import run_jscpd
from rebar.metrics.registry import Unavailable

_ACCRUING_SINCE = "2026-01-01T00:00:00+00:00"
_LOGGER = logging.getLogger(__name__)


def analyze(
    repo_root: Path,
    languages: tuple[str, ...] | None = None,
) -> AnalyzerResult | Unavailable:
    """Return JSCPD duplication totals, or why the command could not run."""

    del languages
    try:
        duplication = run_jscpd(repo_root.resolve())
    except FileNotFoundError:
        return _unavailable("jscpd executable not found")
    except (
        OSError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
    ) as exc:
        return _unavailable(f"could not run jscpd: {exc}")
    return AnalyzerResult(duplication=duplication)


def _unavailable(reason: str) -> Unavailable:
    """Log and build the standard unavailable result."""

    _LOGGER.warning("jscpd unavailable: %s", reason)
    return Unavailable(reason=reason, accruing_since=_ACCRUING_SINCE)
