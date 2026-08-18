"""WS4: the engine must resolve to a REAL on-disk directory (no zipimport).

rebar's engine assets (the ``rebar_reconciler`` package + the Jira capability
probe) are exec'd as real files. ``engine_dir()`` asserts the resolved path is a
real directory and raises a clear RuntimeError otherwise, so a zip-imported /
mispackaged install fails loudly instead of with an opaque import error.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rebar import _engine


def test_engine_dir_is_real_on_disk_directory():
    p = _engine.engine_dir()
    assert p.is_dir(), f"engine_dir() must be a real directory, got {p!s}"
    # The engine assets must be present as real files/dirs: the reconciler, the
    # Jira probe, and the alias wordlist (what engine_env launches/needs).
    assert _engine.wordlist_path().is_file()
    assert (p / "rebar_reconciler").is_dir()
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

    The engine's Python is real ``rebar.*`` subpackages; the library does not
    insert the engine dir onto ``sys.path``. So after ``import rebar``, bare
    ``import ticket_reducer`` (etc.) must fail — those names are not ``rebar``
    modules, and ``rebar_reconciler`` resolves only inside engine subprocesses
    via ``engine_env``'s PYTHONPATH (which this probe strips).

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
    cp = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env)
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

    repo_root = Path(_engine.__file__).resolve().parents[2]
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


def test_engine_submodules_resolve_when_the_tests_unit_shadow_is_active(tmp_path: Path):
    """bug dbb2: engine ``rebar_reconciler.*`` submodules must resolve in ANY session
    that collects ``tests/unit/**``, not only one that collects
    ``tests/unit/rebar_reconciler/**``.

    ``tests/unit/`` has no ``__init__.py``, so pytest's prepend import mode puts it at
    ``sys.path[0]`` for every ``tests/unit`` module it collects — and
    ``tests/unit/rebar_reconciler/__init__.py`` then shadows the engine package of the
    same name. Anything that exec's an engine module standalone (e.g.
    ``tests/scripts/reducer/test_managed_refs.py``'s ``spec_from_file_location`` of
    ``outbound_differ.py``) resolves its ``from rebar_reconciler.… import …`` through
    that shadow, and dies with ``ModuleNotFoundError: No module named
    'rebar_reconciler._loader'`` unless the shadow's ``__path__`` carries the engine
    package.

    Run in a subprocess, and select a ``tests/unit`` module that is NOT under
    ``rebar_reconciler/``: an in-process assertion is masked, because a full-suite run
    always collects ``tests/unit/rebar_reconciler/**`` and therefore always fires that
    directory's own compensation — the invariant would hold even with the fix reverted.
    Same masking hazard, and same subprocess remedy, as
    :func:`test_library_path_exposes_no_generic_top_level_engine_names` above.

    ``-k`` deselects every test in the module named below, so this guard does not
    recurse into itself; the module is imported (which is what creates the shadow) and
    only the reducer test actually runs.
    """
    import subprocess
    import sys

    repo_root = Path(_engine.__file__).resolve().parents[2]
    scripts_test = repo_root / "tests" / "scripts" / "reducer" / "test_managed_refs.py"
    assert scripts_test.is_file(), scripts_test
    assert (repo_root / "tests" / "unit" / "rebar_reconciler" / "__init__.py").is_file(), (
        "precondition: the tests/unit shadow package must exist for this guard to mean "
        "anything — if it is gone, delete this guard rather than letting it pass vacuously"
    )

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    child_basetemp = tmp_path / "mixed-module-pytest"
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(Path(__file__).resolve()),  # a tests/unit module outside rebar_reconciler/
            str(scripts_test),
            "-k",
            "test_unlink_after_compaction_still_propagates_removal",
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(child_basetemp),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )
    combined = cp.stdout + cp.stderr
    assert "No module named 'rebar_reconciler" not in combined, (
        "the tests/unit package shadow hid the engine rebar_reconciler submodules:\n" + combined
    )
    assert cp.returncode == 0, (
        "a mixed tests/unit + tests/scripts/reducer selection failed:\n" + combined
    )
    assert child_basetemp.is_dir(), "nested pytest did not use its parent-owned basetemp"
