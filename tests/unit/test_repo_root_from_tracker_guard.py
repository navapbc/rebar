"""Self-tests for the repo/config-root-from-tracker gate (bug ``2ec7-be89-9b01-496a``).

The gate flags ``os.path.dirname(<tracker>)`` used as a code/config root — a construct that is
correct only for a co-located store and silently disables gates on a ``REBAR_TRACKER_DIR``
relocated one. These tests pin the DISCRIMINATION (the two flagged shapes fail; a resolver
call and prose do not), the ``# repo-root-ok: <reason>`` sanction, the reasonless-marker
diagnostic, the loud handling of an unparseable source, and the gate's own wiring into
``make lint`` — a gate that runs only in CI lets a local verdict be green over a tree CI
rejects.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_repo_root_from_tracker.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import check_repo_root_from_tracker as gate  # noqa: E402


def _scan(tmp_path: Path, source: str) -> tuple[list, list]:
    """Run the gate over a synthetic one-file tree, returning (violations, bare_findings)."""
    src = tmp_path / "src" / "rebar"
    src.mkdir(parents=True)
    (src / "sample.py").write_text(source, encoding="utf-8")
    return gate.find_violations(tmp_path)


# ─────────────────────────── the construct is rejected ───────────────────────────


@pytest.mark.parametrize(
    ("source", "shape"),
    [
        ("import os\nroot = os.path.dirname(tracker)\n", "`tracker`"),
        (
            "import os as _os\nroot = _os.path.dirname(tracker)\n",
            "`tracker`",
        ),
        (
            "import os\nfrom rebar._engine_support import reads\n"
            "root = os.path.dirname(reads.tracker_dir())\n",
            "`reads.tracker_dir(...)`",
        ),
        (
            "from os.path import dirname\nroot = dirname(tracker)\n",
            "`tracker`",
        ),
        (
            "import os\nroot = os.path.dirname(str(tracker))\n",
            "`str(tracker)`",
        ),
        (
            "import os\nfrom rebar._engine_support import reads\n"
            "root = os.path.dirname(str(reads.tracker_dir()))\n",
            "`str(reads.tracker_dir(...))`",
        ),
    ],
)
def test_each_construct_shape_is_rejected(tmp_path: Path, source: str, shape: str) -> None:
    violations, _ = _scan(tmp_path, source)
    assert len(violations) == 1, f"expected one violation, got {[v.text for v in violations]}"
    assert violations[0].shape == shape


def test_the_str_wrapped_variant_is_rejected(tmp_path: Path) -> None:
    """The exact injurious-pugnacious-azurevase line from composer.py that suppressed the
    save-time advisory on a relocated store — the str()-wrapped variant the auspicial guard
    originally missed."""
    violations, _ = _scan(tmp_path, "import os\ncfg_root = os.path.dirname(str(tracker))\n")
    assert len(violations) == 1
    assert violations[0].shape == "`str(tracker)`"


def test_realpath_and_abspath_wrappers_are_not_unwrapped(tmp_path: Path) -> None:
    """The realpath()/abspath() family is a separate, design-laden follow-up; the str()
    extension must NOT flag those wrappers (a per-site root decision, tracked elsewhere)."""
    source = (
        "import os\n"
        "a = os.path.dirname(os.path.realpath(tracker))\n"
        "b = os.path.dirname(os.path.abspath(tracker))\n"
    )
    violations, bare = _scan(tmp_path, source)
    assert violations == [] and bare == [], (
        f"must not flag realpath/abspath wrappers: {[v.text for v in violations + bare]}"
    )


def test_the_real_defect_shape_is_rejected(tmp_path: Path) -> None:
    """The exact line from ``transition.py:275`` that disabled the gate in production."""
    violations, _ = _scan(tmp_path, "import os\nrepo_root_str = os.path.dirname(tracker)\n")
    assert len(violations) == 1


# ───────────────────────── legitimate uses are NOT rejected ─────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "import os\nlogdir = os.path.dirname(log_path)\n", id="dirname-of-non-tracker"
        ),
        pytest.param(
            "import os\nparent = os.path.dirname(ticket_dir)\n", id="dirname-of-ticket-dir"
        ),
        pytest.param(
            '"""os.path.dirname(tracker) is wrong on a relocated store."""\n', id="docstring"
        ),
        pytest.param("x = 1  # never os.path.dirname(tracker)\n", id="comment"),
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
        "import os\nroot = os.path.dirname(tracker)  # repo-root-ok: cwd IS the store here\n",
    )
    assert violations == [] and bare == []


def test_a_marker_on_the_line_above_sanctions_it(tmp_path: Path) -> None:
    violations, bare = _scan(
        tmp_path,
        "import os\n# repo-root-ok: detached child whose cwd is the store\n"
        "root = os.path.dirname(tracker)\n",
    )
    assert violations == [] and bare == []


def test_a_reasonless_marker_is_reported_as_reasonless_not_as_unmarked(tmp_path: Path) -> None:
    violations, bare = _scan(
        tmp_path, "import os\nroot = os.path.dirname(tracker)  # repo-root-ok\n"
    )
    assert violations == []
    assert len(bare) == 1


def test_an_empty_reason_does_not_sanction(tmp_path: Path) -> None:
    violations, bare = _scan(
        tmp_path, "import os\nroot = os.path.dirname(tracker)  # repo-root-ok:   \n"
    )
    assert len(violations) + len(bare) == 1


# ─────────────────────── an unparseable source is loud, not silent ───────────────────────


def test_unparseable_source_is_reported_not_skipped(tmp_path: Path) -> None:
    """A production module that fails to parse could hide a fresh site — flag it."""
    violations, _ = _scan(tmp_path, "import os\nroot = os.path.dirname(tracker)\ndef (:\n")
    assert len(violations) == 1
    assert violations[0].shape == "unparseable source"


# ─────────────────────────── the real tree, and wiring ───────────────────────────


def test_the_repository_is_clean() -> None:
    """The drained tree passes: the run_sweep site now RESOLVES its config root, so zero
    config-root sites are deferred behind a sanction (bug scathing-custommade-bobcat)."""
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT)], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_no_config_root_site_is_deferred_behind_a_sanction() -> None:
    """AC#3 (bug scathing-custommade-bobcat): the ``# repo-root-ok:`` allowlist holds ZERO
    deferred CONFIG-root sites. The run_sweep detached-child site — the one the auspicial guard
    deferred — now resolves the code root, so its sanction is gone. The ONLY sanctions left name
    reads of the STORE's OWN git repo (``git -C tracker`` for tickets_branch / tickets_remote in
    _store/sync.py), which are legitimately rooted at the store and are NOT config-root reads."""
    src = _REPO_ROOT / "src" / "rebar"
    sanctioned: dict[str, list[int]] = {}
    for path in sorted(src.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if gate.MARKER in line and line.split(gate.MARKER, 1)[1].strip():
                sanctioned.setdefault(str(path.relative_to(_REPO_ROOT)), []).append(lineno)

    compact_trigger = "src/rebar/_commands/compact_trigger.py"
    assert compact_trigger not in sanctioned, (
        "the run_sweep config-root site must be RESOLVED, not deferred behind a repo-root-ok "
        f"sanction; found sanction(s) at {sanctioned.get(compact_trigger)!r}"
    )
    assert set(sanctioned) <= {"src/rebar/_store/sync.py"}, (
        "the only remaining repo-root-ok sanctions may be the store's-own-git-repo reads in "
        f"_store/sync.py (tickets_branch / tickets_remote); found others: {sorted(sanctioned)}"
    )


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
    assert "scripts/check_repo_root_from_tracker.py" in "\n".join(body), (
        "`make lint` does not invoke the repo-root-from-tracker gate"
    )
