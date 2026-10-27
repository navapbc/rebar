"""The one resolution rule for the eval corpus root (epic e47b slice 1).

The eval harnesses must run for a CLIENT project supplying its own corpus, so the
replay/ledger output roots resolve through ``[llm.evals] corpus_root`` instead of the
CWD-relative literals they used to hard-code (those paths have since moved under
``evals/``, epic e47b slice 2).

This module names the pre-move root once, inside ``test_no_hard_coded_roots``: an
assertion that a string is absent has to spell the string out. That one occurrence is
why the repo-wide grep carves this file out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar.llm.config import (
    DEFAULT_EVALS_CORPUS_ROOT,
    default_ledger_path,
    evals_corpus_root,
    replay_dir,
)

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A git repo with no ``[llm.evals]`` table, so each test picks its own layer."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBAR_LLM_EVALS_CORPUS_ROOT", raising=False)
    monkeypatch.delenv("REBAR_LLM_CONFIG_FILE", raising=False)


def test_default_is_dot_rebar_evals_under_the_repo_root(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    assert evals_corpus_root(str(project)) == project / DEFAULT_EVALS_CORPUS_ROOT


def test_config_file_table_beats_the_default(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    (project / "rebar.toml").write_text(
        '[llm.evals]\ncorpus_root = "corpus/from-file"\n', encoding="utf-8"
    )
    assert evals_corpus_root(str(project)) == project / "corpus" / "from-file"


def test_env_beats_the_config_file(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    (project / "rebar.toml").write_text(
        '[llm.evals]\ncorpus_root = "corpus/from-file"\n', encoding="utf-8"
    )
    monkeypatch.setenv("REBAR_LLM_EVALS_CORPUS_ROOT", "corpus/from-env")
    assert evals_corpus_root(str(project)) == project / "corpus" / "from-env"


def test_cli_override_beats_env(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``parse_cli_overrides`` splits on the FIRST dot, so ``evals.corpus_root`` arrives
    as one sub-key under section ``llm`` — the same key the file layer is flattened to."""
    import rebar.config as root_config

    _clear_env(monkeypatch)
    monkeypatch.setenv("REBAR_LLM_EVALS_CORPUS_ROOT", "corpus/from-env")
    monkeypatch.setattr(
        root_config, "cli_overrides_for", lambda name: {"evals.corpus_root": "corpus/from-cli"}
    )
    assert evals_corpus_root(str(project)) == project / "corpus" / "from-cli"


def test_an_absolute_value_is_returned_unchanged(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    absolute = tmp_path / "elsewhere" / "corpus"
    monkeypatch.setenv("REBAR_LLM_EVALS_CORPUS_ROOT", str(absolute))
    assert evals_corpus_root(str(project)) == absolute


def test_sub_paths_are_fixed_under_the_corpus_root(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("REBAR_LLM_EVALS_CORPUS_ROOT", "corpus")
    replay = project / "corpus" / "plan-review-gate" / "replay"
    assert replay_dir(str(project)) == replay
    assert default_ledger_path(str(project)) == replay / "ledger.jsonl"


def test_no_hard_coded_roots() -> None:
    """No corpus root is hard-coded in the eval package; every path resolves through config.

    Two guards. First, the pre-move root must be gone entirely — slice 2 moved the corpus
    out of ``docs/`` and repointed every live reference, so any surviving literal is a
    missed repoint. Second, the post-move root may appear only in ``corpus.py``'s
    path-only docstring cite; anywhere else it would be a reinstated hard-coded default.
    """

    def _files(pattern: str) -> list[str]:
        hits = subprocess.run(
            ["git", "grep", "-l", pattern, "--", "src/rebar/llm/evals"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
        return sorted(hits)

    assert _files("docs/experiments") == []
    assert _files("evals/plan-review-gate") == ["src/rebar/llm/evals/plan_replay/corpus.py"]
