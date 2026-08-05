"""Single source for the module-size computation the CI gate enforces.

The CI ``Module-size gate`` measures ``find src/rebar -name '*.py' | xargs wc -l`` against
``.github/module-size-limit.txt``. Two tests need that same computation:
``tests/unit/test_module_size_contract.py`` mirrors the gate in-process, and
``tests/unit/metrics/test_scc_module_size_reconciliation_heldout.py`` asserts the scc-backed
size metric agrees with it (rebar-ticket c5b3-1b8a-08dd-40af). It lives HERE, in one place,
rather than being duplicated or imported across test directories:

* Duplicating it would give the repository two definitions of the module-size rule, which is
  precisely the drift the reconciliation test exists to prevent.
* ``tests/`` is importable by DELIBERATE guarantee -- ``tests/conftest.py`` inserts this
  directory into ``sys.path`` so tests can share helpers next to it. Every other bare-name
  test import in this repo is same-directory; reaching across test directories instead relies
  on pytest's incidental basedir insertion, which silently changes if an ``__init__.py`` is
  added or removed. Paths here anchor to ``__file__``, never to the working directory, so the
  helpers resolve identically under ``pytest`` and ``python -m pytest`` from any cwd.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "rebar"
LIMIT_FILE = REPO_ROOT / ".github" / "module-size-limit.txt"


def read_limit() -> int:
    """The module-size limit, read from the single source the CI gate also reads."""
    return int(LIMIT_FILE.read_text(encoding="utf-8").strip())


def compute_over_cap_modules(src_root: Path, *, cap: int) -> dict[str, int]:
    """Repo-relative POSIX path -> LOC for every ``src_root`` ``*.py`` file OVER ``cap`` lines.

    Line counting uses ``text.count("\\n")`` to match the CI gate's ``wc -l`` semantics exactly.
    ``__pycache__`` is skipped, mirroring the gate's ``grep -v __pycache__``."""
    over: dict[str, int] = {}
    for path in src_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        loc = path.read_text(encoding="utf-8", errors="surrogateescape").count("\n")
        if loc > cap:
            over[path.relative_to(REPO_ROOT).as_posix()] = loc
    return over


def gate_file_loc() -> dict[str, int]:
    """Repo-relative POSIX path -> ``wc -l`` LOC for EVERY file the CI gate measures.

    The gate's own file set, before any cap is applied -- what the scc-backed module-size
    metric must reproduce file-for-file and line-for-line."""
    return {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text(
            encoding="utf-8", errors="surrogateescape"
        ).count("\n")
        for path in SRC_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    }
