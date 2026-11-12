"""A degraded gate verdict names a proven build/config skew (misogynic-scheelite-solitaire).

A gate reads its ``[tool.rebar.llm]`` config from the attested snapshot but runs the locally
installed code. When ``main`` moves to models that need matching code (06b3b1a5230 moved the
config to the 5.5 ids and, in the same commit, stopped sending ``temperature`` to them), an older
install sends a request the provider rejects. The verdict then carried only the provider 400 in
``coverage.llm_error``. These tests pair a newer attested config commit with an older running
build and require the degraded verdict to name the skew and the update remedy.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from rebar._snapshot import SOURCE_ATTESTED, SOURCE_LOCAL
from rebar._snapshot.repo_snapshot import SnapshotHandle
from rebar.llm import build_drift, gate_source

pytestmark = pytest.mark.unit

_PROVIDER_400 = (
    "ValidationException: temperature is not supported for model us.anthropic.claude-sonnet-5-5"
)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[tuple[Path, str, str]]:
    """``old`` has the 4.x model config; ``new`` moves it to 5.5 (the attested config)."""
    build_drift.reset_warned()  # the recorded finding is process-global
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    shas = []
    for model in (
        "bedrock:us.anthropic.claude-sonnet-4-6",
        "bedrock:us.anthropic.claude-sonnet-5-5",
    ):
        (root / "rebar.toml").write_text(f'[llm]\nmodel = "{model}"\n', encoding="utf-8")
        _git(root, "add", "rebar.toml")
        _git(root, "commit", "-q", "-m", model)
        shas.append(_git(root, "rev-parse", "HEAD"))
    yield root, shas[0], shas[1]
    build_drift.reset_warned()


def _degraded() -> dict:
    return {
        "verdict": "INDETERMINATE",
        "coverage": {"llm_ran": False, "llm_error": _PROVIDER_400},
    }


def _resolve_as_build(monkeypatch: pytest.MonkeyPatch, build: str, root: Path, pinned: str):
    """Run the handle-resolution drift probe as a build at ``build`` pinning ``pinned``."""
    monkeypatch.setattr(build_drift, "running_build_sha", lambda: build)
    monkeypatch.setattr(build_drift, "_resolve_repo_root", lambda _root: str(root))
    build_drift.warn_if_behind(pinned, str(root))


def test_older_build_names_the_skew_and_remedy_on_a_degraded_verdict(
    monkeypatch: pytest.MonkeyPatch, repo: tuple[Path, str, str]
) -> None:
    root, old, new = repo
    _resolve_as_build(monkeypatch, old[:11], root, new)
    handle = SnapshotHandle(path=root, sha=new, source=SOURCE_ATTESTED)

    result = gate_source.annotate_result(_degraded(), handle)

    skew = result["coverage"].get("build_drift")
    assert skew is not None, result
    assert skew["pinned_sha"] == new and skew["build_sha"] == old
    assert skew["commits_behind"] == 1
    assert "make install" in skew["remedy"] and new[:9] in skew["remedy"]
    assert result["coverage"]["llm_error"] == _PROVIDER_400, "the provider error is kept verbatim"


def test_build_at_the_pinned_ref_adds_nothing(
    monkeypatch: pytest.MonkeyPatch, repo: tuple[Path, str, str]
) -> None:
    root, _old, new = repo
    _resolve_as_build(monkeypatch, new[:11], root, new)
    handle = SnapshotHandle(path=root, sha=new, source=SOURCE_ATTESTED)

    result = gate_source.annotate_result(_degraded(), handle)

    assert "build_drift" not in result["coverage"]


def test_a_verdict_where_the_model_ran_is_not_annotated(
    monkeypatch: pytest.MonkeyPatch, repo: tuple[Path, str, str]
) -> None:
    root, old, new = repo
    _resolve_as_build(monkeypatch, old[:11], root, new)
    handle = SnapshotHandle(path=root, sha=new, source=SOURCE_ATTESTED)
    ran = {"verdict": "PASS", "coverage": {"llm_ran": True}}

    result = gate_source.annotate_result(ran, handle)

    assert "build_drift" not in result["coverage"]


def test_a_local_source_gate_pins_nothing_and_is_not_annotated(
    monkeypatch: pytest.MonkeyPatch, repo: tuple[Path, str, str]
) -> None:
    root, old, new = repo
    _resolve_as_build(monkeypatch, old[:11], root, new)
    handle = SnapshotHandle(path=root, sha=None, source=SOURCE_LOCAL)

    result = gate_source.annotate_result(_degraded(), handle)

    assert "build_drift" not in result["coverage"]
