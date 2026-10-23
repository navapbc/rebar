"""Test ADR number uniqueness, marker bijection, and link integrity.

Each ADR filename has one matching numbered marker. Duplicate numbers, mismatched
marker content, or dangling ADR links produce gate errors. Held-out tests cover edges.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.scripts

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_adr_numbers.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_adr_numbers", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _adr(adr_dir: Path, number: str, slug: str, body: str = "") -> str:
    """Write an ADR file ``NNNN-slug.md`` and return its filename."""
    name = f"{number}-{slug}.md"
    (adr_dir / name).write_text(body or f"# {number}: {slug}\n", encoding="utf-8")
    return name


def _marker(adr_dir: Path, number: str, content: str) -> None:
    markers = adr_dir / ".numbers"
    markers.mkdir(exist_ok=True)
    (markers / number).write_text(content + "\n", encoding="utf-8")


def _valid_tree(tmp_path: Path) -> tuple[Path, Path]:
    """A minimal but realistic clean docs tree: docs/ with docs/adr/ inside it."""
    docs = tmp_path / "docs"
    adr = docs / "adr"
    adr.mkdir(parents=True)
    for num, slug in (
        ("0001", "first-decision"),
        ("0002", "second-decision"),
        ("0070", "moved-one"),
    ):
        fname = _adr(adr, num, slug)
        _marker(adr, num, fname)
    # a docs page that links to an existing ADR by its real slug -> referentially sound
    (docs / "architecture.md").write_text(
        "See [the first decision](adr/0001-first-decision.md) for context.\n",
        encoding="utf-8",
    )
    return adr, docs


def test_clean_tree_passes(gate: ModuleType, tmp_path: Path) -> None:
    adr, docs = _valid_tree(tmp_path)
    errors = gate.check(adr, docs)
    assert errors == [], f"expected a clean tree to pass, got: {errors}"


def test_clean_tree_main_returns_zero(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adr, docs = _valid_tree(tmp_path)
    rc = gate.main(["--adr-dir", str(adr), "--docs-dir", str(docs)])
    assert rc == 0


def test_markers_dirname_is_dot_numbers(gate: ModuleType) -> None:
    assert gate.MARKERS_DIRNAME == ".numbers"


def test_default_adr_dir_points_at_repo(gate: ModuleType) -> None:
    assert gate.DEFAULT_ADR_DIR.name == "adr"
    assert gate.DEFAULT_ADR_DIR.parent.name == "docs"
