"""Test ADR number uniqueness, marker bijection, and link integrity.

Each ADR filename has one matching numbered marker. Duplicate numbers, mismatched
marker content, or dangling ADR links produce gate errors. Held-out tests cover edges.

Held-out oracles merged from test_check_adr_numbers_heldout: Held-out edge suite for the ADR-
number uniqueness / bijection gate (story 0743).
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


# ── held-out oracles (merged from test_check_adr_numbers_heldout) ──

REPO_ROOT = Path(__file__).resolve().parents[2]


_SCRIPT_HELDOUT = REPO_ROOT / "scripts" / "check_adr_numbers.py"


@pytest.fixture(scope="module")
def gate_heldout() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_adr_numbers_heldout", _SCRIPT_HELDOUT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _adr_heldout(adr_dir: Path, number: str, slug: str) -> str:
    name = f"{number}-{slug}.md"
    (adr_dir / name).write_text(f"# {number}: {slug}\n", encoding="utf-8")
    return name


def _base_tree(tmp_path: Path) -> tuple[Path, Path]:
    docs = tmp_path / "docs"
    adr = docs / "adr"
    adr.mkdir(parents=True)
    for num, slug in (("0001", "alpha"), ("0002", "beta")):
        _marker(adr, num, _adr_heldout(adr, num, slug))
    return adr, docs


def _has_error(errors: list[str], *needles: str) -> bool:
    return any(all(n.lower() in e.lower() for n in needles) for e in errors)


def test_duplicate_number_fails(gate_heldout: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    # a second ADR file numbered 0001
    _adr_heldout(adr, "0001", "alpha-twin")
    errors = gate_heldout.check(adr, docs)
    assert errors, "two ADR files sharing 0001 must fail"
    assert _has_error(errors, "0001"), errors


def test_missing_marker_fails(gate_heldout: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    _adr_heldout(adr, "0003", "gamma")  # no marker written
    errors = gate_heldout.check(adr, docs)
    assert _has_error(errors, "0003"), errors


def test_orphan_marker_fails(gate_heldout: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    _marker(adr, "0099", "0099-ghost.md")  # no such ADR file
    errors = gate_heldout.check(adr, docs)
    assert _has_error(errors, "0099"), errors


def test_marker_content_mismatch_fails(gate_heldout: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    # marker 0001 names the wrong filename
    _marker(adr, "0001", "0001-not-the-real-slug.md")
    errors = gate_heldout.check(adr, docs)
    assert errors, "marker whose content != ADR filename must fail"


def test_marker_number_prefix_mismatch_fails(gate_heldout: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    # marker file named 0007 but names an ADR whose number is 0002
    _marker(adr, "0007", "0002-beta.md")
    errors = gate_heldout.check(adr, docs)
    assert errors, "marker whose NNNN name != referenced ADR's number must fail"


def test_duplicate_marker_content_fails(gate_heldout: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    # a spurious second marker whose content duplicates 0001's ADR filename
    _marker(adr, "0005", "0001-alpha.md")
    errors = gate_heldout.check(adr, docs)
    assert errors, "two markers sharing content must fail"


def test_dangling_reference_fails(gate_heldout: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    (docs / "guide.md").write_text("see [gone](adr/0042-removed-decision.md)\n", encoding="utf-8")
    errors = gate_heldout.check(adr, docs)
    assert _has_error(errors, "0042"), errors


def test_valid_reference_passes(gate_heldout: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    (docs / "guide.md").write_text("see [alpha](adr/0001-alpha.md)\n", encoding="utf-8")
    assert gate_heldout.check(adr, docs) == []


def test_intra_adr_dangling_reference_fails(gate_heldout: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    # an ADR body cross-linking a sibling that does not exist
    (adr / "0002-beta.md").write_text(
        "# 0002: beta\n\nSupersedes [older](0033-vanished.md).\n", encoding="utf-8"
    )
    errors = gate_heldout.check(adr, docs)
    assert _has_error(errors, "0033"), errors


def test_main_returns_one_on_violation(gate_heldout: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    _adr_heldout(adr, "0001", "alpha-twin")  # duplicate number
    rc = gate_heldout.main(["--adr-dir", str(adr), "--docs-dir", str(docs)])
    assert rc == 1
