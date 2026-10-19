"""Keep rebar and lock modules importable without POSIX fcntl (0b31).

Unconditional imports once broke Windows collection. Match guarded sibling
modules. Off-POSIX locking need not work, but imports must. A child process
sets sys.modules['fcntl']=None so already imported parent modules cannot
hide an import failure."""

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
