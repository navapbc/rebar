"""Self-tests for the repo-root-from-package-location gate (bug ``c0b9-33b0-f968-450d``).

The gate flags ``Path(__file__).resolve().parents[N]`` used as a checkout root — a construct
that is correct only under an editable install and silently resolves ``<venv>/lib/pythonX`` on
a non-editable/wheel install (the reconciler reconciled the wrong tree, bug c0b9). These tests
pin the DISCRIMINATION (the subscript root-climb fails; the singular ``.parent`` package-data
idiom, a resolver call, and prose do not), the ``# pkg-root-ok:`` / ``# pkg-root-seam:``
sanctions, the reasonless-marker diagnostic, the loud handling of an unparseable source, the
real drained tree, and the gate's own wiring into ``make lint`` — a gate that runs only in CI
lets a local verdict be green over a tree CI rejects.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_repo_root_from_package.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import check_repo_root_from_package as gate  # noqa: E402


def _scan(tmp_path: Path, source: str) -> tuple[list, list]:
    """Run the gate over a synthetic one-file tree, returning (violations, bare_findings)."""
    src = tmp_path / "src" / "rebar"
    src.mkdir(parents=True)
    (src / "sample.py").write_text(source, encoding="utf-8")
    return gate.find_violations(tmp_path)


# ─────────────────────────── the construct is rejected ───────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "from pathlib import Path\nroot = Path(__file__).parents[2]\n", id="bare-parents"
        ),
        pytest.param(
            "from pathlib import Path\nroot = Path(__file__).resolve().parents[2]\n",
            id="resolve-parents",
        ),
        pytest.param(
            "from pathlib import Path\nroot = Path(__file__).absolute().parents[3]\n",
            id="absolute-parents",
        ),
        pytest.param(
            "from pathlib import Path\nroot = str(Path(__file__).resolve().parents[4])\n",
            id="str-wrapped",
        ),
        pytest.param(
            "from pathlib import Path\nroot = env or Path(__file__).resolve().parents[4]\n",
            id="inside-boolean-fallback",
        ),
        pytest.param(
            "import pathlib\nroot = pathlib.Path(__file__).resolve().parents[2]\n",
            id="module-qualified-Path",
        ),
    ],
)
def test_each_construct_shape_is_rejected(tmp_path: Path, source: str) -> None:
    violations, bare = _scan(tmp_path, source)
    assert len(violations) == 1, f"expected one violation, got {[v.text for v in violations]}"
    assert violations[0].shape == "`Path(__file__).parents[...]`"
    assert bare == []


def test_the_real_defect_shape_is_rejected(tmp_path: Path) -> None:
    """The exact fetcher.py line that resolved ``repo_root=<venv>/lib/pythonX`` on a
    non-editable CI leg (the c0b9 sibling)."""
    violations, _ = _scan(
        tmp_path,
        "from pathlib import Path\n"
        "repo_root = Path(repo_root_env() or Path(__file__).resolve().parents[4])\n",
    )
    assert len(violations) == 1


# ───────────────────────── legitimate uses are NOT rejected ─────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "from pathlib import Path\nassets = Path(__file__).parent / 'editor_assets'\n",
            id="singular-parent-package-data",
        ),
        pytest.param(
            "from pathlib import Path\nspec = Path(__file__).resolve().parent.parent / 'x'\n",
            id="parent-parent-package-data",
        ),
        pytest.param("root = config.reconciler_repo_root()\n", id="validated-resolver-call"),
        pytest.param("parent = other.parents[2]\n", id="parents-on-a-non-file-path"),
        pytest.param(
            '"""Path(__file__).resolve().parents[2] is wrong under a wheel install."""\n',
            id="docstring",
        ),
        pytest.param("x = 1  # never Path(__file__).parents[2]\n", id="comment"),
    ],
)
def test_legitimate_and_prose_are_not_flagged(tmp_path: Path, source: str) -> None:
    violations, bare = _scan(tmp_path, source)
    assert violations == [] and bare == [], (
        f"must not be flagged: {[v.text for v in violations + bare]}"
    )


# ────────────────────────────── the sanction ──────────────────────────────


def test_a_reasoned_marker_sanctions_the_line(tmp_path: Path) -> None:
    violations, bare = _scan(
        tmp_path,
        "from pathlib import Path\n"
        "p = Path(__file__).resolve().parents[3]  # pkg-root-ok: package parent for sys.path\n",
    )
    assert violations == [] and bare == []


def test_a_marker_on_the_line_above_sanctions_it(tmp_path: Path) -> None:
    violations, bare = _scan(
        tmp_path,
        "from pathlib import Path\n"
        "# pkg-root-ok: package parent prepended to sys.path, not a checkout root\n"
        "p = Path(__file__).resolve().parents[3]\n",
    )
    assert violations == [] and bare == []


def test_a_reasonless_marker_is_reported_as_reasonless_not_as_unmarked(tmp_path: Path) -> None:
    violations, bare = _scan(
        tmp_path,
        "from pathlib import Path\nroot = Path(__file__).parents[2]  # pkg-root-ok\n",
    )
    assert violations == []
    assert len(bare) == 1


def test_an_empty_reason_does_not_sanction(tmp_path: Path) -> None:
    violations, bare = _scan(
        tmp_path,
        "from pathlib import Path\nroot = Path(__file__).parents[2]  # pkg-root-ok:   \n",
    )
    assert len(violations) + len(bare) == 1


# ────────────────────────── the shared seam marker ──────────────────────────


def test_the_seam_marker_sanctions_the_validated_resolver(tmp_path: Path) -> None:
    """``# pkg-root-seam: <reason>`` exempts THE single validated resolver
    (``config.reconciler_repo_root``), whose ``parents[2]`` candidate is used only after
    ``_is_repo_checkout`` confirms a real checkout — the one place allowed to spell the
    construct, which every reconciler surface routes through."""
    violations, bare = _scan(
        tmp_path,
        "from pathlib import Path\n"
        "# pkg-root-seam: validated by _is_repo_checkout before use\n"
        "package_root = Path(__file__).resolve().parents[2]\n",
    )
    assert violations == [] and bare == []


def test_a_reasonless_seam_marker_is_reported(tmp_path: Path) -> None:
    violations, bare = _scan(
        tmp_path,
        "from pathlib import Path\n# pkg-root-seam\nroot = Path(__file__).parents[2]\n",
    )
    assert violations == []
    assert len(bare) == 1


def test_the_seam_marker_does_not_count_as_a_repo_root_ok_sanction() -> None:
    """The seam marker is DISTINCT from ``# pkg-root-ok`` so each carries its own intent."""
    assert "# pkg-root-ok" not in gate.SEAM_MARKER
    assert gate.BARE_MARKER not in "# pkg-root-seam:"


# ─────────────────────── an unparseable source is loud, not silent ───────────────────────


def test_unparseable_source_is_reported_not_skipped(tmp_path: Path) -> None:
    """A production module that fails to parse could hide a fresh site — flag it."""
    violations, _ = _scan(
        tmp_path, "from pathlib import Path\nroot = Path(__file__).parents[2]\ndef (:\n"
    )
    assert len(violations) == 1
    assert violations[0].shape == "unparseable source"


# ─────────────────────────── the real tree, and wiring ───────────────────────────


def test_the_repository_is_clean() -> None:
    """The drained tree passes (only the two sanctioned sites remain)."""
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT)], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_make_lint_invokes_the_gate() -> None:
    """A CI-only gate lets a local verdict be green over a tree CI rejects."""
    text = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    body: list[str] = []
    in_target = False
    for line in text.splitlines():
        if re.match(r"^lint:", line):
            in_target = True
            continue
        if in_target and re.match(r"^[A-Za-z0-9_.-]+:", line):
            break
        if in_target:
            body.append(line)
    assert "scripts/check_repo_root_from_package.py" in "\n".join(body), (
        "`make lint` does not invoke the repo-root-from-package gate"
    )


def test_the_only_sanctioned_sites_are_the_resolver_seam_and_the_pkg_parent() -> None:
    """The sanction exists for a genuine non-checkout package-location derivation, never as a
    place to park a fix. Exactly two real-tree sites may carry a marker on a ``.parents[`` line:
    ``config.py`` (the validated resolver seam) and ``rebar_reconciler/__main__.py`` (the
    sys.path package parent). A third sanctioned site is a regression of the drained class.
    """
    sanctioned_files: set[str] = set()
    for path in sorted((_REPO_ROOT / gate.SCAN_ROOT).rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines):
            if ".parents[" not in line:
                continue
            window = " ".join(lines[max(0, idx - 1) : idx + 1])
            if gate.BARE_MARKER in window or "# pkg-root-seam" in window:
                sanctioned_files.add(str(path.relative_to(_REPO_ROOT)))
    assert sanctioned_files == {
        "src/rebar/config.py",
        "src/rebar/_engine/rebar_reconciler/__main__.py",
    }, f"unexpected sanctioned package-location site(s): {sorted(sanctioned_files)}"
