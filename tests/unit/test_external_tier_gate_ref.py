"""The external tier's scratch repo must resolve the suite-wide attested gate ref.

Regression guard for bug ``8070-3f8b-8e71-483b``: every live test in
``tests/external/`` builds its store with that tier's ``rebar_repo`` fixture. Since
``tests/conftest.py`` defaults the whole suite to ``REBAR_GATE_SOURCE=attested`` /
``REBAR_GATE_REF=HEAD``, a scratch repo with an UNBORN ``HEAD`` (``git init`` and no
commit) makes every gate op fail closed with ``SnapshotRefError`` before it ever reaches
the behaviour under test — which is what turned the weekly external-integration canary red.

This lives in the default tier on purpose. ``tests/conftest.py`` auto-marks every item
under ``tests/external/`` as ``external`` (excluded from the default run and gated on live
credentials), so a guard placed there could only fail in the very weekly job that already
went ten days unread. Here it costs no credentials and no billable model call, and it
exercises the tier's REAL repo construction via the extracted
``build_scratch_rebar_repo`` helper rather than a local re-implementation of it.
"""

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
    """The external tier's repo must resolve an attested snapshot at ``ref=HEAD``.

    Pins the mechanism end to end: build the repo exactly as the live tier does, then ask
    for a gate handle under the suite's own attested/``HEAD`` default. Before the fix this
    raised ``SnapshotRefError`` ("cannot resolve ref 'HEAD' ..."); it must now hand back an
    attested handle pinned to a real commit SHA.
    """
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
