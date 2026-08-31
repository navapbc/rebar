"""``fcntl`` is POSIX-only; importing rebar must not require it.

Regression for bug ``0b31-aeb5-e734-41c9``. ``fcntl`` has no Windows build, so an
*unconditional* module-top ``import fcntl`` makes the whole module unimportable off POSIX.
Four modules did this, and because ``import rebar`` transitively reaches one of them
(``rebar._commands.doctor_locks``), the entire package — and therefore every test module
that imports it — failed to *collect* on Windows with ``ModuleNotFoundError: No module
named 'fcntl'``. Six sibling modules already guard the import
(``try: import fcntl / except ImportError: fcntl = None``); these four must match.

The contract this pins: with ``fcntl`` absent, ``import rebar`` and each lock module still
import (collectability). Locking itself is not required to work off POSIX — Windows is not
a declared support target — only that import/collection succeeds.

Proven by simulating an ``fcntl``-less interpreter in a child process
(``sys.modules['fcntl'] = None`` makes ``import fcntl`` raise ``ImportError``, exactly as a
missing module does). A subprocess keeps the parent's already-imported rebar modules from
masking the result.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

# The four modules whose unguarded module-top ``import fcntl`` broke off-POSIX collection.
FCNTL_LOCK_MODULES = [
    "rebar._store.lock",
    "rebar._store.git_locking",
    "rebar.reducer.marker",
    "rebar._commands.doctor_locks",
]


def _import_under_no_fcntl(target: str) -> subprocess.CompletedProcess[str]:
    """Import *target* in a fresh interpreter where ``fcntl`` is unavailable."""
    code = textwrap.dedent(
        f"""
        import sys
        sys.modules['fcntl'] = None  # simulate a non-POSIX platform: import fcntl -> ImportError
        import importlib
        importlib.import_module({target!r})
        print('IMPORT_OK')
        """
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("module", FCNTL_LOCK_MODULES)
def test_lock_module_imports_without_fcntl(module: str) -> None:
    """Each lock module must import when ``fcntl`` is absent (collectability off POSIX)."""
    result = _import_under_no_fcntl(module)
    assert result.returncode == 0, f"{module} failed to import without fcntl:\n{result.stderr}"
    assert result.stdout.strip().endswith("IMPORT_OK")


def test_import_rebar_without_fcntl() -> None:
    """``import rebar`` must succeed without ``fcntl`` — otherwise ALL collection dies."""
    result = _import_under_no_fcntl("rebar")
    assert result.returncode == 0, f"import rebar failed without fcntl:\n{result.stderr}"
    assert result.stdout.strip().endswith("IMPORT_OK")


def test_fcntl_available_here_control() -> None:
    """Negative control: on this POSIX host ``fcntl`` is present and the modules import
    normally through the real module — the fix must not change supported-platform behavior."""
    import importlib

    fcntl = importlib.import_module("fcntl")
    assert hasattr(fcntl, "flock")
    for module in FCNTL_LOCK_MODULES:
        importlib.import_module(module)


# ─────────────────────── structural guard self-tests (scripts/check_fcntl_import_guard.py) ──
#
# The regression tests above pin runtime behaviour. This block pins the deterministic AST
# gate that keeps the class from re-entering: it must FLAG an unconditional module-scope
# ``import fcntl`` (true positive), and must NOT flag the collection-safe idioms — a lazy
# in-function import, a ``try``-guard, an ``if sys.platform`` guard, or a sanctioned line
# (true negatives / refactor survival).

import re  # noqa: E402
from pathlib import Path  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import check_fcntl_import_guard as gate  # noqa: E402


def _scan_source(tmp_path: Path, source: str) -> list:
    src = tmp_path / "src" / "rebar"
    src.mkdir(parents=True, exist_ok=True)
    (src / "sample.py").write_text(source, encoding="utf-8")
    return gate.find_violations(src)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import fcntl\n", id="bare-import"),
        pytest.param("import os\nimport fcntl\nimport sys\n", id="import-in-group"),
        pytest.param("from fcntl import flock\n", id="from-import"),
        pytest.param("import fcntl as _f\n", id="import-as"),
        pytest.param("with open('x') as f:\n    import fcntl\n", id="unconditional-with"),
    ],
)
def test_guard_flags_unconditional_module_scope_import(tmp_path: Path, source: str) -> None:
    """True positive: an unconditional module-scope fcntl import is flagged."""
    findings = _scan_source(tmp_path, source)
    assert len(findings) == 1
    assert findings[0].module == "fcntl"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n", id="try-guard"
        ),
        pytest.param(
            "import sys\nif sys.platform != 'win32':\n    import fcntl\nelse:\n    fcntl = None\n",
            id="if-platform-guard",
        ),
        pytest.param(
            "def _lock():\n    import fcntl\n    return fcntl.LOCK_EX\n", id="lazy-in-function"
        ),
        pytest.param("import fcntl  # fcntl-guard-ok: TYPE_CHECKING-only shim\n", id="sanctioned"),
        pytest.param("import os\nimport sys\n", id="no-fcntl-at-all"),
    ],
)
def test_guard_passes_collection_safe_idioms(tmp_path: Path, source: str) -> None:
    """True negative / refactor survival: guarded, lazy, and sanctioned forms are allowed."""
    assert _scan_source(tmp_path, source) == []


def test_guard_is_clean_on_the_real_tree() -> None:
    """The shipped ``src/rebar`` tree must pass the gate (no unguarded fcntl imports)."""
    assert gate.find_violations(_REPO_ROOT / "src" / "rebar") == []


def test_make_lint_invokes_the_guard() -> None:
    """A gate that runs only in CI lets a local green verdict stand over a tree CI rejects."""
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
    assert "scripts/check_fcntl_import_guard.py" in "\n".join(body), (
        "`make lint` does not invoke the fcntl import guard"
    )
