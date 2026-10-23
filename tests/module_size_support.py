"""Share the CI module-size computation with contract and metrics tests.

Paths anchor to this file so editable and installed runs resolve the same source tree.
The shared helper prevents its two consumers from implementing different limits.
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
