"""Happy-path contract for the scc LOC analyzer (ticket 18f9)."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from rebar.metrics.analyzer import AnalyzerResult

pytestmark = pytest.mark.unit


def _subject() -> ModuleType:
    try:
        return importlib.import_module("rebar.metrics.analyzers.scc_loc")
    except ModuleNotFoundError:
        pytest.fail("the scc LOC analyzer is not implemented")


def test_parse(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    payload = [
        {
            "Name": "Python",
            "Files": [
                {"Location": str(repo_root / "src" / "small.py"), "Code": 100},
            ],
        },
        {
            "Name": "TypeScript",
            "Files": [
                {"Location": str(repo_root / "web" / "large.ts"), "Code": 850},
            ],
        },
    ]

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    result = _subject().analyze(repo_root, run=run)

    assert isinstance(result, AnalyzerResult)
    assert result.loc == {
        "files": {"src/small.py": 100, "web/large.ts": 850},
        "max_loc": 850,
    }
    assert result.complexity is None
    assert result.duplication is None
