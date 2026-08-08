"""Happy-path contract for the ADR-number uniqueness / bijection gate (story 0743).

The gate proves ADR numbers are unique and collision-proof. Every ADR file
``docs/adr/NNNN-slug.md`` is paired with a per-number marker file
``docs/adr/.numbers/NNNN`` whose content is that ADR's filename. Two ADRs claiming
one number produce an add/add marker conflict that Rebase-If-Necessary cannot
auto-resolve (that is git's job); this gate is the CI backstop that asserts, on the
merged tree, that the bijection actually holds and that no cross-reference dangles.

API contract (scripts/check_adr_numbers.py):
  - DEFAULT_ADR_DIR: Path                                  # repo docs/adr
  - MARKERS_DIRNAME: str                                   # ".numbers"
  - check(adr_dir: Path, docs_dir: Path | None = None) -> list[str]  # error strings, [] == clean
  - main(argv: list[str] | None = None) -> int            # 0 clean, 1 failures

Rules (each violation contributes >=1 error string):
  1. bijection: every ADR file has a marker; every marker maps to exactly one ADR.
  2. marker content == the ADR filename it names.
  3. no two ADR files share a number.
  4. no two markers share content.
  5. a marker's ``NNNN`` name matches the number-prefix of the ADR it names.
  6. referential integrity: every ``adr/NNNN-slug(.md)`` markdown link target under
     ``docs/`` (and inside ``docs/adr/*.md`` bodies) resolves to an existing ADR file.

Edge shapes (each rule's failure path, exit codes, CI wiring, real-tree cleanliness)
are HELD OUT in test_check_adr_numbers_heldout.py.
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
