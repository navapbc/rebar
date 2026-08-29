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


def test_realpath_and_abspath_wrappers_are_unwrapped(tmp_path: Path) -> None:
    """flowered-basaltic-beagle: the realpath()/abspath() family names the same directory as
    the bare form, so ``dirname(os.path.realpath(tracker))`` composes the store's parent
    exactly like ``dirname(tracker)`` — now that every legitimate store-repo site routes
    through the shared seam, these must be flagged."""
    source = (
        "import os\n"
        "a = os.path.dirname(os.path.realpath(tracker))\n"
        "b = os.path.dirname(os.path.abspath(tracker))\n"
    )
    violations, bare = _scan(tmp_path, source)
    assert [v.shape for v in violations] == [
        "`os.path.realpath(tracker)`",
        "`os.path.abspath(tracker)`",
    ], [v.text for v in violations]
    assert bare == []


def test_storepaths_canonical_is_flagged(tmp_path: Path) -> None:
    """feisty-intense-mandrill: ``dirname(StorePaths(tracker).canonical)`` composes the repo
    root from the resolved store path — the exact construct the guard exists to catch, and its
    former blind spot. The bare/str-wrapped ``.canonical`` spellings are covered too."""
    source = (
        "import os\n"
        "a = os.path.dirname(StorePaths(tracker).canonical)\n"
        "b = os.path.dirname(str(StorePaths(tracker).canonical))\n"
    )
    violations, bare = _scan(tmp_path, source)
    assert [v.shape for v in violations] == [
        "`StorePaths(tracker).canonical`",
        "`str(StorePaths(tracker).canonical)`",
    ], [v.text for v in violations]
    assert bare == []


def test_bare_storepaths_canonical_argument_is_not_flagged(tmp_path: Path) -> None:
    """Passing ``StorePaths(tracker).canonical`` as an argument (the canonical store path a
    detached child receives) is legitimate — only COMPOSING a root from it via ``dirname`` is
    the defect, so the un-``dirname``-wrapped use must not be flagged."""
    violations, bare = _scan(tmp_path, "spawn(StorePaths(tracker).canonical)\n")
    assert violations == [] and bare == []


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


# ────────────────────────── the shared seam marker ──────────────────────────


def test_the_seam_marker_sanctions_the_realpath_seam(tmp_path: Path) -> None:
    """flowered-basaltic-beagle: ``# repo-root-seam: <reason>`` exempts THE single canonical
    helper that resolves the store's own repo root — the one place allowed to spell the
    composition, which every other store-repo site routes through."""
    violations, bare = _scan(
        tmp_path,
        "import os\n"
        "# repo-root-seam: THE store-repo-root derivation; every store-repo site routes here.\n"
        "root = os.path.dirname(os.path.realpath(tracker))\n",
    )
    assert violations == [] and bare == []


def test_a_reasonless_seam_marker_is_reported(tmp_path: Path) -> None:
    """A seam marker is a sanction too, so a reasonless one is a bare-marker finding — the
    seam must document WHY it is the one exempt derivation."""
    violations, bare = _scan(
        tmp_path,
        "import os\n# repo-root-seam\nroot = os.path.dirname(os.path.realpath(tracker))\n",
    )
    assert violations == []
    assert len(bare) == 1


def test_the_seam_marker_does_not_count_as_a_repo_root_ok_sanction() -> None:
    """The seam marker is DISTINCT from ``# repo-root-ok`` so the deferral invariant below can
    keep counting only the latter — a seam line must not read as a deferred config-root site."""
    assert "# repo-root-ok" not in gate.SEAM_MARKER
    assert gate.BARE_MARKER not in "# repo-root-seam:"


# ─────────────────────── an unparseable source is loud, not silent ───────────────────────


def test_unparseable_source_is_reported_not_skipped(tmp_path: Path) -> None:
    """A production module that fails to parse could hide a fresh site — flag it."""
    violations, _ = _scan(tmp_path, "import os\nroot = os.path.dirname(tracker)\ndef (:\n")
    assert len(violations) == 1
    assert violations[0].shape == "unparseable source"


# ─────────────────────────── the real tree, and wiring ───────────────────────────


def test_the_repository_is_clean() -> None:
    """The drained tree passes."""
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
    assert "scripts/check_repo_root_from_tracker.py" in "\n".join(body), (
        "`make lint` does not invoke the repo-root-from-tracker gate"
    )


def test_no_config_root_site_is_deferred_behind_a_sanction() -> None:
    """``scathing-custommade-bobcat`` AC#3: the allowlist holds no DEFERRED config-root site.

    The sanction exists for a path that genuinely is NOT a code root, never as a place to park
    a fix. ``compact_trigger.run_sweep`` was the one deferred entry — sanctioned while "how does
    a detached child learn its code root" looked like an open spawn-contract question — and it
    is now resolved with the bare resolver like every other config reader. What may still carry
    a marker is ``_store/sync.py``, whose reads name the branch and remote of the STORE's own
    git repo; anything else is a regression of the deferral this ticket drained.
    """
    marked: dict[str, list[str]] = {}
    for path in sorted((_REPO_ROOT / gate.SCAN_ROOT).rglob("*.py")):
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if gate.BARE_MARKER in line
        ]
        if lines:
            marked[str(path.relative_to(_REPO_ROOT))] = lines
    assert "src/rebar/_commands/compact_trigger.py" not in marked, (
        "the detached compaction sweep is sanctioned again — it must RESOLVE its code root "
        f"(config.repo_root_or_none()), not compose it from the store: {marked}"
    )
    assert set(marked) <= {"src/rebar/_store/sync.py"}, (
        "a new repo-root-ok sanction appeared outside the store's-own-git-repo reads; a "
        f"config root must be resolved, not deferred behind a marker: {sorted(marked)}"
    )


def test_exactly_one_shared_store_root_seam_exists() -> None:
    """flowered-basaltic-beagle: the store-root derivation lives in exactly ONE place — the
    shared ``rebar._proc.store_repo_root`` seam — so no caller hand-rolls ``dirname(tracker)``.
    A ``# repo-root-seam`` marker anywhere else is a second choke point and a regression.
    """
    seam_files: dict[str, list[str]] = {}
    for path in sorted((_REPO_ROOT / gate.SCAN_ROOT).rglob("*.py")):
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if "# repo-root-seam" in line
        ]
        if lines:
            seam_files[str(path.relative_to(_REPO_ROOT))] = lines
    assert set(seam_files) == {"src/rebar/_proc.py"}, (
        f"the shared store-root seam must be the single site in _proc.py: {sorted(seam_files)}"
    )
    assert len(seam_files["src/rebar/_proc.py"]) == 1, (
        f"more than one seam marker in _proc.py: {seam_files['src/rebar/_proc.py']}"
    )
