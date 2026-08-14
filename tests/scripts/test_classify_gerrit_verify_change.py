"""Fail-closed path classification for the Gerrit Verified docs-only route."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.scripts

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "classify_gerrit_verify_change.py"


def _load():
    spec = importlib.util.spec_from_file_location("classify_gerrit_verify_change", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


classifier = _load()


@pytest.mark.parametrize(
    "paths",
    [
        ("README.md",),
        ("CHANGELOG.md", "docs/README.md"),
        ("docs/adr/0092-docs-only-verify.md", "docs/adr/.numbers/0092"),
        ("docs/architecture/overview.svg",),
    ],
)
def test_documentation_only_paths_use_the_reduced_route(paths: tuple[str, ...]) -> None:
    assert classifier.classify_paths(paths) == classifier.DOCS_ONLY


@pytest.mark.parametrize(
    "path",
    [
        "src/rebar/store.py",
        "scripts/release.sh",
        ".github/workflows/gerrit-verify.yaml",
        ".github/actions/docs-gates/action.yml",
        "scripts/classify_gerrit_verify_change.py",
        "pyproject.toml",
        "uv.lock",
        "tests/unit/test_store.py",
        "tests/fixtures/example.json",
        "rebar.toml",
        ".rebar/criteria_routing.json",
        "AGENTS.md",
        "docs/experiments/probe.py",
        "docs/experiments/result.json",
        "unrecognized/document.md",
    ],
)
def test_every_code_config_policy_or_unknown_path_uses_full_verify(path: str) -> None:
    assert classifier.classify_paths((path,)) == classifier.FULL


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"docs/README.md",
        b"docs/README.md\0\0",
        b"/docs/README.md\0",
        b"docs/../README.md\0",
        b"docs\\README.md\0",
        b"docs/\xff.md\0",
    ],
)
def test_empty_or_malformed_path_data_uses_full_verify(raw: bytes) -> None:
    assert classifier.classify_paths0(raw) == classifier.FULL


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def test_repository_classifier_reads_exact_head_parent_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "CI")
    (repo / "src").mkdir()
    (repo / "src" / "old.py").write_text("old = True\n", encoding="utf-8")
    _commit(repo, "base")

    (repo / "docs").mkdir()
    (repo / "docs" / "README.md").write_text("# docs\n", encoding="utf-8")
    _commit(repo, "docs")

    assert classifier.changed_paths(repo) == ("docs/README.md",)
    assert classifier.classify_repository(repo) == classifier.DOCS_ONLY


def test_code_to_docs_rename_keeps_the_deleted_code_path_and_uses_full(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "CI")
    (repo / "src").mkdir()
    (repo / "src" / "old.py").write_text("old = True\n", encoding="utf-8")
    _commit(repo, "base")

    (repo / "docs").mkdir()
    _git(repo, "mv", "src/old.py", "docs/old.md")
    _commit(repo, "rename")

    paths = classifier.changed_paths(repo)
    assert paths is not None and set(paths) == {"src/old.py", "docs/old.md"}
    assert classifier.classify_repository(repo) == classifier.FULL


def test_parentless_repository_uses_full_verify(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "CI")
    (repo / "README.md").write_text("# docs\n", encoding="utf-8")
    _commit(repo, "only commit")
    assert classifier.classify_repository(repo) == classifier.FULL


def test_classifier_exception_uses_full_verify(tmp_path: Path) -> None:
    def broken_reader(_repo: Path) -> tuple[str, ...]:
        raise RuntimeError("injected")

    assert classifier.classify_repository(tmp_path, diff_reader=broken_reader) == classifier.FULL
