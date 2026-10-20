"""WS4: the engine must resolve to a REAL on-disk directory (no zipimport).

rebar's engine assets (the Jira capability probe and the bundled resources) are
exec'd and read as real files. ``engine_dir()`` asserts the resolved path is a
real directory and raises a clear RuntimeError otherwise, so a zip-imported /
mispackaged install fails loudly instead of with an opaque import error.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from _nested_pytest import nested_basetemp, run_nested_pytest
from _repo_root import REPO_ROOT

from rebar import _engine


def test_engine_dir_is_real_on_disk_directory():
    p = _engine.engine_dir()
    assert p.is_dir(), f"engine_dir() must be a real directory, got {p!s}"
    # The engine assets must be present as real files: the Jira probe and the alias
    # wordlist (what engine_env launches/needs).
    assert _engine.wordlist_path().is_file()
    assert (p / "jira-capability-probe.py").is_file()


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

    The engine's Python is real ``rebar.*`` subpackages (the reconciler is
    ``rebar.reconciler``); the library does not insert the engine dir onto
    ``sys.path``. So after ``import rebar``, bare ``import ticket_reducer`` (etc.)
    must fail — those names are not ``rebar`` modules — and the retired top-level
    ``rebar_reconciler`` name must not resolve either.

    Run in a clean subprocess with PYTHONPATH stripped, so the only thing making
    ``rebar`` importable is the real install, mirroring a library consumer.
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
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, check=False
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

    repo_root = REPO_ROOT
    assert (repo_root / "pyproject.toml").is_file(), repo_root

    builder = hatchling_wheel.WheelBuilder(str(repo_root))
    built = list(builder.build(directory=str(tmp_path)))
    wheels = [p for p in built if str(p).endswith(".whl")]
    assert wheels, f"no wheel produced, got: {built}"

    with zipfile.ZipFile(wheels[0]) as zf:
        bad = [n for n in zf.namelist() if n.endswith((".pyc", ".pyo")) or "__pycache__" in n]
    assert not bad, f"wheel shipped compiled bytecode: {bad[:20]}"


def test_wheel_ships_author_guides(tmp_path):
    """Every registered author guide (`rebar explain plan` / `review` / `commit-trailer` / …)
    must ride in the wheel.

    They are the canonical source (moved out of repo-root ``docs/``) precisely so an installed
    rebar can serve them; if the wheel dropped one, ``explain_guide`` would 500 on real installs.
    Builds the wheel in-process (same pattern as the bytecode guard) and asserts every file in
    ``AUTHOR_GUIDES`` ships under ``rebar/_guides/`` — driven by the registry so a newly-added
    guide is packaging-guarded automatically.
    """
    import zipfile

    from rebar.llm.plan_review.registry import AUTHOR_GUIDES

    hatchling_wheel = pytest.importorskip("hatchling.builders.wheel")

    repo_root = REPO_ROOT
    builder = hatchling_wheel.WheelBuilder(str(repo_root))
    built = list(builder.build(directory=str(tmp_path)))
    wheels = [p for p in built if str(p).endswith(".whl")]
    assert wheels, f"no wheel produced, got: {built}"

    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())
    for guide in AUTHOR_GUIDES.values():
        assert f"rebar/_guides/{guide}" in names, (
            f"wheel is missing packaged guide rebar/_guides/{guide}"
        )


_RECONCILER_PROBE_MODULES = ("rebar.reconciler.classify", "rebar.reconciler.adapters.jira.acli")


def test_reconciler_submodules_resolve_to_the_package_source() -> None:
    """``rebar.reconciler`` submodules, imported from a ``tests/unit`` module, are the
    package's own files under ``src/rebar/reconciler/`` — never a test-tree module of the
    same name (``tests/unit/rebar_reconciler/classify/`` is a test package)."""
    import importlib

    reconciler_src = (REPO_ROOT / "src" / "rebar" / "reconciler").resolve()
    for name in _RECONCILER_PROBE_MODULES:
        module = importlib.import_module(name)
        origin = Path(module.__file__ or "").resolve()
        assert origin.is_relative_to(reconciler_src), f"{name} resolved to {origin}"


def test_engine_submodules_resolve_from_a_tests_unit_module(tmp_path: Path):
    """Resolve ``rebar.reconciler`` submodules from a fresh session that collects a
    ``tests/unit`` module outside ``tests/unit/rebar_reconciler/``.

    The child collects this file next to a ``tests/scripts`` module, so the in-process
    probe above runs with only that collection's import state — no full-suite import
    order can mask a resolution failure. ``-k`` selects the probe and one scripts test
    and deselects this test, preventing recursion.
    """

    repo_root = REPO_ROOT
    scripts_test = repo_root / "tests" / "scripts" / "reducer" / "test_managed_refs.py"
    assert scripts_test.is_file(), scripts_test

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    child_basetemp = nested_basetemp(tmp_path)
    cp = run_nested_pytest(
        tmp_path,
        str(Path(__file__).resolve()),  # a tests/unit module outside rebar_reconciler/
        str(scripts_test),
        "-k",
        "test_reconciler_submodules_resolve_to_the_package_source"
        " or test_unlink_after_compaction_still_propagates_removal",
        "-q",
        "-p",
        "no:randomly",
        env=env,
        cwd=repo_root,
    )
    combined = cp.stdout + cp.stderr
    assert "ModuleNotFoundError" not in combined, (
        "a rebar.reconciler submodule failed to import from a tests/unit module:\n" + combined
    )
    assert cp.returncode == 0, (
        "a mixed tests/unit + tests/scripts/reducer selection failed:\n" + combined
    )
    assert "2 passed" in combined, "the nested selection did not run both probes:\n" + combined
    assert child_basetemp.is_dir(), "nested pytest did not use its parent-owned basetemp"
