"""External-tier scratch repos must resolve attested HEAD (8070).

The tier defaults to an attested HEAD. An unborn scratch repo made every
external gate fail SnapshotRefError before its intended behavior. This default-
tier guard uses the build_scratch_rebar_repo helper, so it runs without
weekly service credentials or model calls."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from rebar.llm import gate_source

_EXTERNAL_CONFTEST = Path(__file__).resolve().parents[1] / "external" / "conftest.py"


def _load_external_conftest() -> ModuleType:
    """Import ``tests/external/conftest.py`` by path (conftests are not importable by name)."""
    spec = importlib.util.spec_from_file_location("_rebar_external_conftest", _EXTERNAL_CONFTEST)
    assert spec is not None and spec.loader is not None, _EXTERNAL_CONFTEST
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_external_scratch_repo_resolves_attested_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build the tier's scratch repo and request its default attested HEAD.
    The gate must pin a commit SHA instead of raising SnapshotRefError."""
    monkeypatch.setenv("REBAR_GATE_SOURCE", "attested")
    monkeypatch.setenv("REBAR_GATE_REF", "HEAD")

    repo = tmp_path / "repo"
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    _load_external_conftest().build_scratch_rebar_repo(repo)

    # Precondition: HEAD is a real commit on the CODE branch, not unborn.
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    assert head.returncode == 0, (
        "the external tier's scratch repo has an UNBORN HEAD; every attested gate op in "
        f"tests/external/ fails closed before reaching its subject. git said: {head.stderr!r}"
    )

    handle = gate_source.resolve_gate_handle(None, None, str(repo))

    assert handle.source == "attested", handle
    assert handle.sha == head.stdout.strip(), handle
