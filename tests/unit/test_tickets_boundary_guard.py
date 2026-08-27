"""Self-tests for the tickets-store boundary gate (bug ``0514-92e0-e6c4-4304``).

The gate exists because ``.tickets-tracker`` appears ~77 times in ``src/rebar`` and only a
minority of those are defects. A gate that flagged all of them would be abandoned within a
week, so these tests pin the DISCRIMINATION — composition fails, prose does not — as tightly
as they pin the failure itself. They also pin the gate's own wiring into ``make lint``: a gate
that runs only in CI lets a local verdict be green over a tree CI rejects.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_tickets_boundary.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import check_tickets_boundary as gate  # noqa: E402


def _scan(tmp_path: Path, source: str) -> tuple[list, list]:
    """Run the gate over a synthetic one-file tree, returning (violations, bare_findings)."""
    src = tmp_path / "src" / "rebar"
    src.mkdir(parents=True)
    (src / "sample.py").write_text(source, encoding="utf-8")
    return gate.find_violations(tmp_path)


# ─────────────────────────── composition is rejected ───────────────────────────


@pytest.mark.parametrize(
    ("source", "shape"),
    [
        ('from pathlib import Path\nx = root / ".tickets-tracker"\n', "path join with `/`"),
        (
            'import os\nx = os.path.join(root, ".tickets-tracker")\n',
            "`os.path.join(...)` argument",
        ),
        (
            'from pathlib import Path\nD = Path(".tickets-tracker/.bridge_state/x.json")\n',
            "`Path(...)` argument",
        ),
        ('TRACKER_DIR = ".tickets-tracker"\n', "dir-name constant `TRACKER_DIR`"),
    ],
)
def test_each_composing_shape_is_rejected(tmp_path: Path, source: str, shape: str) -> None:
    violations, _ = _scan(tmp_path, source)
    assert len(violations) == 1, f"expected one violation, got {[v.text for v in violations]}"
    assert violations[0].shape == shape


def test_the_real_repo_defect_shape_is_rejected(tmp_path: Path) -> None:
    """The exact line from ``last_pass.py:89`` that broke ``bridge_status`` in production."""
    violations, _ = _scan(tmp_path, 'path = repo_root / ".tickets-tracker" / ".env-id"\n')
    assert len(violations) == 1


# ───────────────────────────── prose is NOT rejected ─────────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('"""The store lives in .tickets-tracker/ by default."""\n', id="docstring"),
        pytest.param("x = 1  # the .tickets-tracker worktree\n", id="comment"),
        pytest.param(
            'raise RuntimeError("commit .tickets-tracker and re-run the pass")\n',
            id="error-message",
        ),
        pytest.param(
            'parser.add_argument("--t", help="Path to the .tickets-tracker directory.")\n',
            id="argparse-help",
        ),
        pytest.param(
            'def f():\n    """tracker_dir: Path to the .tickets-tracker directory."""\n',
            id="param-docstring",
        ),
    ],
)
def test_prose_is_not_flagged(tmp_path: Path, source: str) -> None:
    """Prose composes nothing. Flagging it would train contributors to mark noise."""
    violations, bare = _scan(tmp_path, source)
    assert violations == [] and bare == [], (
        f"prose must not be flagged: {[v.text for v in violations + bare]}"
    )


# ────────────────────────────── the sanction ──────────────────────────────


def test_a_reasoned_marker_sanctions_the_line(tmp_path: Path) -> None:
    violations, bare = _scan(
        tmp_path,
        'x = root / ".tickets-tracker"  # tickets-boundary-ok: temp dir this code just made\n',
    )
    assert violations == [] and bare == []


def test_a_marker_on_the_line_above_sanctions_it(tmp_path: Path) -> None:
    violations, bare = _scan(
        tmp_path,
        "# tickets-boundary-ok: the default name inside the resolver\n"
        'x = root / ".tickets-tracker"\n',
    )
    assert violations == [] and bare == []


def test_a_reasonless_marker_is_reported_as_reasonless_not_as_unmarked(tmp_path: Path) -> None:
    """The pre-existing bare form must produce its OWN diagnostic.

    Seven of the thirteen defects this gate drained carried a bare marker, so reporting them
    as merely "unmarked" would tell a contributor to add a marker that is already there.
    """
    violations, bare = _scan(tmp_path, 'x = root / ".tickets-tracker"  # tickets-boundary-ok\n')
    assert violations == []
    assert len(bare) == 1


def test_an_empty_reason_does_not_sanction(tmp_path: Path) -> None:
    """``# tickets-boundary-ok:`` with nothing after the colon is not a reason."""
    violations, bare = _scan(tmp_path, 'x = root / ".tickets-tracker"  # tickets-boundary-ok:   \n')
    assert len(violations) + len(bare) == 1


# ─────────────────────────── the real tree, and wiring ───────────────────────────


def test_the_repository_is_clean() -> None:
    """The drained tree passes. This is the regression guard for all thirteen sites."""
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
    assert "scripts/check_tickets_boundary.py" in "\n".join(body), (
        "`make lint` does not invoke the tickets-boundary gate"
    )
