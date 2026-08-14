"""Held-out edge suite for the ADR-number uniqueness / bijection gate (story 0743).

Pins every failure path the happy-path contract only gestures at: duplicate number,
missing/orphan marker, wrong marker content, marker/number mismatch, a dangling
``adr/NNNN-slug`` reference, the exit-code contract, the CI wiring, and cleanliness
of the REAL repository tree at landing (the renumbered corpus must satisfy its own
gate). Held out from the implementation subagent so a green result proves the gate,
not the test.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.scripts

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "check_adr_numbers.py"
_DOCS_ACTION = REPO_ROOT / ".github" / "actions" / "docs-gates" / "action.yml"
_REAL_ADR = REPO_ROOT / "docs" / "adr"
_REAL_DOCS = REPO_ROOT / "docs"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_adr_numbers_heldout", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _adr(adr_dir: Path, number: str, slug: str) -> str:
    name = f"{number}-{slug}.md"
    (adr_dir / name).write_text(f"# {number}: {slug}\n", encoding="utf-8")
    return name


def _marker(adr_dir: Path, number: str, content: str) -> None:
    markers = adr_dir / ".numbers"
    markers.mkdir(exist_ok=True)
    (markers / number).write_text(content + "\n", encoding="utf-8")


def _base_tree(tmp_path: Path) -> tuple[Path, Path]:
    docs = tmp_path / "docs"
    adr = docs / "adr"
    adr.mkdir(parents=True)
    for num, slug in (("0001", "alpha"), ("0002", "beta")):
        _marker(adr, num, _adr(adr, num, slug))
    return adr, docs


def _has_error(errors: list[str], *needles: str) -> bool:
    return any(all(n.lower() in e.lower() for n in needles) for e in errors)


# ---- Rule 3: duplicate number ------------------------------------------------
def test_duplicate_number_fails(gate: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    # a second ADR file numbered 0001
    _adr(adr, "0001", "alpha-twin")
    errors = gate.check(adr, docs)
    assert errors, "two ADR files sharing 0001 must fail"
    assert _has_error(errors, "0001"), errors


# ---- Rule 1: missing marker (ADR without marker) -----------------------------
def test_missing_marker_fails(gate: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    _adr(adr, "0003", "gamma")  # no marker written
    errors = gate.check(adr, docs)
    assert _has_error(errors, "0003"), errors


# ---- Rule 1: orphan marker (marker without ADR) ------------------------------
def test_orphan_marker_fails(gate: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    _marker(adr, "0099", "0099-ghost.md")  # no such ADR file
    errors = gate.check(adr, docs)
    assert _has_error(errors, "0099"), errors


# ---- Rule 2: wrong marker content --------------------------------------------
def test_marker_content_mismatch_fails(gate: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    # marker 0001 names the wrong filename
    _marker(adr, "0001", "0001-not-the-real-slug.md")
    errors = gate.check(adr, docs)
    assert errors, "marker whose content != ADR filename must fail"


# ---- Rule 5: marker name vs number-prefix mismatch ---------------------------
def test_marker_number_prefix_mismatch_fails(gate: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    # marker file named 0007 but names an ADR whose number is 0002
    _marker(adr, "0007", "0002-beta.md")
    errors = gate.check(adr, docs)
    assert errors, "marker whose NNNN name != referenced ADR's number must fail"


# ---- Rule 4: two markers naming the same ADR ---------------------------------
def test_duplicate_marker_content_fails(gate: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    # a spurious second marker whose content duplicates 0001's ADR filename
    _marker(adr, "0005", "0001-alpha.md")
    errors = gate.check(adr, docs)
    assert errors, "two markers sharing content must fail"


# ---- Rule 6: dangling cross-reference ----------------------------------------
def test_dangling_reference_fails(gate: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    (docs / "guide.md").write_text("see [gone](adr/0042-removed-decision.md)\n", encoding="utf-8")
    errors = gate.check(adr, docs)
    assert _has_error(errors, "0042"), errors


def test_valid_reference_passes(gate: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    (docs / "guide.md").write_text("see [alpha](adr/0001-alpha.md)\n", encoding="utf-8")
    assert gate.check(adr, docs) == []


def test_intra_adr_dangling_reference_fails(gate: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    # an ADR body cross-linking a sibling that does not exist
    (adr / "0002-beta.md").write_text(
        "# 0002: beta\n\nSupersedes [older](0033-vanished.md).\n", encoding="utf-8"
    )
    errors = gate.check(adr, docs)
    assert _has_error(errors, "0033"), errors


# ---- exit-code contract ------------------------------------------------------
def test_main_returns_one_on_violation(gate: ModuleType, tmp_path: Path) -> None:
    adr, docs = _base_tree(tmp_path)
    _adr(adr, "0001", "alpha-twin")  # duplicate number
    rc = gate.main(["--adr-dir", str(adr), "--docs-dir", str(docs)])
    assert rc == 1


# ---- CI wiring ---------------------------------------------------------------
def test_ci_wires_the_gate() -> None:
    text = _DOCS_ACTION.read_text(encoding="utf-8")
    assert "check_adr_numbers.py" in text, "shared CI action must invoke the ADR-number gate"


# ---- real-tree cleanliness at landing ----------------------------------------
def test_real_repo_tree_is_clean(gate: ModuleType) -> None:
    errors = gate.check(_REAL_ADR, _REAL_DOCS)
    assert errors == [], f"the landed ADR corpus must satisfy its own gate: {errors}"


def test_real_tree_has_no_duplicate_numbers() -> None:
    nums = [p.name[:4] for p in _REAL_ADR.glob("*.md") if p.name[:4].isdigit()]
    dupes = sorted({n for n in nums if nums.count(n) > 1})
    assert dupes == [], f"duplicate ADR numbers remain: {dupes}"


def test_real_tree_marker_per_adr() -> None:
    markers = _REAL_ADR / ".numbers"
    assert markers.is_dir(), "docs/adr/.numbers/ must exist"
    adr_nums = {p.name[:4] for p in _REAL_ADR.glob("*.md") if p.name[:4].isdigit()}
    marker_nums = {p.name for p in markers.iterdir() if p.is_file() and p.name[:4].isdigit()}
    assert adr_nums == marker_nums, (
        f"marker/ADR number sets differ: only-adr={adr_nums - marker_nums}, "
        f"only-marker={marker_nums - adr_nums}"
    )


def test_real_check_script_runs_as_subprocess() -> None:
    proc = subprocess.run(
        ["python", str(_SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"gate failed on real tree:\n{proc.stdout}\n{proc.stderr}"
