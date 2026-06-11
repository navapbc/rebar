"""WS4: the engine must resolve to a REAL on-disk directory (no zipimport).

rebar's engine is bash + python helpers exec'd as real files. ``engine_dir()``
asserts the resolved path is a real directory and raises a clear RuntimeError
otherwise, so a zip-imported / mispackaged install fails loudly instead of with
an opaque bash error.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rebar import _engine


def test_engine_dir_is_real_on_disk_directory():
    p = _engine.engine_dir()
    assert p.is_dir(), f"engine_dir() must be a real directory, got {p!s}"
    # The dispatcher and the alias wordlist must be present as real files.
    assert _engine.dispatcher().is_file()
    assert _engine.wordlist_path().is_file()


def test_engine_dir_rejects_non_directory(monkeypatch):
    """If importlib.resources resolves the engine to a non-directory (e.g. a
    zipimport-backed path), engine_dir() raises a clear RuntimeError."""
    import importlib.resources

    _engine.engine_dir.cache_clear()
    monkeypatch.setattr(
        importlib.resources, "files", lambda _pkg: Path("/nonexistent/zipimported/rebar")
    )
    with pytest.raises(RuntimeError, match="real on-disk directory"):
        _engine.engine_dir()
    _engine.engine_dir.cache_clear()  # restore real resolution for other tests


# ── Packaging / import-hygiene guards (ticket fare-rant-clasp) ────────────────


def test_library_path_exposes_no_generic_top_level_engine_names():
    """AC1: importing the library must not make generic engine module names
    importable as top-level packages.

    The engine's Python is now real ``rebar.*`` subpackages; the library no
    longer inserts the engine dir onto ``sys.path``. So after ``import rebar``,
    bare ``import ticket_reducer`` (etc.) must fail — those names only resolve
    inside engine subprocesses (via ``engine_env``'s PYTHONPATH compat shims).

    Run in a clean subprocess: this tier's conftest deliberately puts the engine
    dir on ``sys.path`` for the engine unit tests, which would mask the check.
    We also strip PYTHONPATH so the only thing making ``rebar`` importable is the
    real install, mirroring a library consumer.
    """
    import subprocess
    import sys

    probe = (
        "import importlib\n"
        "import rebar\n"
        "from rebar import _native, _reads\n"
        "names = ['ticket_reducer','ticket_graph','ticket_reads',"
        "'ticket_resolver','ticket_output','rebar_reconciler']\n"
        "leaked = []\n"
        "for n in names:\n"
        "    try:\n"
        "        importlib.import_module(n); leaked.append(n)\n"
        "    except ImportError:\n"
        "        pass\n"
        "assert not leaked, leaked\n"
        "import rebar.reducer, rebar.graph\n"
        "assert rebar.reduce_ticket is rebar.reducer.reduce_ticket\n"
        "print('OK')\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    cp = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env
    )
    assert cp.returncode == 0, (
        "generic top-level engine names leaked onto the library import path:\n"
        f"stdout={cp.stdout!r}\nstderr={cp.stderr!r}"
    )


def test_wheel_contains_no_compiled_bytecode(tmp_path):
    """AC2: the built wheel must contain no ``__pycache__`` / ``.pyc`` / ``.pyo``.

    The wheel target force-includes the whole engine dir via ``artifacts``, so a
    stray ``__pycache__`` could ride along. ``pyproject.toml``'s ``exclude``
    guards against it; this test builds the wheel in-process (no network/build
    isolation) and proves nothing compiled shipped. We first import the library
    so ``src/rebar/__pycache__`` exists — the exclusion is genuinely exercised.
    """
    import zipfile

    import rebar  # noqa: F401  (generate __pycache__ next to the sources)

    hatchling_wheel = pytest.importorskip("hatchling.builders.wheel")

    # _engine.__file__ = <repo>/src/rebar/_engine.py -> parents[2] is <repo>.
    repo_root = Path(_engine.__file__).resolve().parents[2]
    assert (repo_root / "pyproject.toml").is_file(), repo_root

    builder = hatchling_wheel.WheelBuilder(str(repo_root))
    built = list(builder.build(directory=str(tmp_path)))
    wheels = [p for p in built if str(p).endswith(".whl")]
    assert wheels, f"no wheel produced, got: {built}"

    with zipfile.ZipFile(wheels[0]) as zf:
        bad = [
            n
            for n in zf.namelist()
            if n.endswith((".pyc", ".pyo")) or "__pycache__" in n
        ]
    assert not bad, f"wheel shipped compiled bytecode: {bad[:20]}"
