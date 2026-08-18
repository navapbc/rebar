"""Held-out oracle for the build-provenance hook fix (story 6168).

`python -m build` builds an sdist, then builds the wheel FROM the extracted sdist (which
has no `.git`). The old hook baked `COMMIT = git rev-parse --short HEAD` = None on that
path, so the published WHEEL lost its provenance. The fix is a four-step precedence:
REBAR_BUILD_COMMIT env → preserve an existing non-null COMMIT (install-from-sdist) → git
short SHA → None; with a release-context fail-fast when the env var is set but empty.

Tests assert OBSERVABLE behaviour: the COMMIT baked into a REAL built wheel/sdist, the
build process exit code, and the helper's return/raise — never internals.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from _build_provenance_fixture import materialize_build_hook_package
from _subprocess_env import subprocess_env

import rebar

REPO = Path(rebar.__file__).resolve().parents[2]


def _build(tree: Path, outdir: Path, env_extra: dict) -> subprocess.CompletedProcess:

    env = subprocess_env()
    env.pop("REBAR_BUILD_COMMIT", None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(outdir), str(tree)],
        capture_output=True,
        text=True,
        env=env,
    )


def _wheel_commit(wheel: Path) -> str | None:
    with zipfile.ZipFile(wheel) as zf:
        name = next(n for n in zf.namelist() if n.endswith("rebar/_build_info.py"))
        ns: dict = {}
        exec(zf.read(name).decode(), ns)
        return ns.get("COMMIT")


@pytest.fixture(scope="session")
def built_artifacts(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Build the shared successful prerequisite once per pytest worker/session."""
    root = tmp_path_factory.mktemp("build-provenance")
    tree = root / "src"
    materialize_build_hook_package(REPO, tree)
    out = root / "dist"
    cp = _build(tree, out, {"REBAR_BUILD_COMMIT": "abc1234"})
    assert cp.returncode == 0, f"build failed: {cp.stderr[-2000:]}"
    wheel = next(out.glob("*.whl"), None)
    sdist = next(out.glob("*.tar.gz"), None)
    assert wheel is not None, "no wheel produced"
    assert sdist is not None, "no sdist produced"
    return wheel, sdist


# ── helper precedence (HAPPY — defines the contract) ──────────────────────────
def _helper():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_hb", REPO / "hatch_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_helper_env_var_wins() -> None:
    mod = _helper()
    assert (
        mod._resolve_build_commit(REPO, existing="oldsha0", env={"REBAR_BUILD_COMMIT": "abc1234"})
        == "abc1234"
    )


def test_helper_preserves_existing_when_no_env() -> None:
    mod = _helper()
    # No env, an existing non-null baked COMMIT is preserved (install-from-sdist path).
    assert mod._resolve_build_commit(Path("/nonexistent"), existing="baked77", env={}) == "baked77"


# ══════════════════════════════════════════════════════════════════════════════
#  HELD-OUT ORACLE — real builds + fail-fast
# ══════════════════════════════════════════════════════════════════════════════
def test_helper_raises_when_env_set_but_empty() -> None:
    mod = _helper()
    assert hasattr(mod, "_resolve_build_commit"), "the hook must expose a testable helper"
    # A set-but-empty env var is a release-context error: raise a real error (NOT return None,
    # and NOT an AttributeError from a missing helper).
    with pytest.raises((ValueError, RuntimeError, SystemExit, OSError)):
        mod._resolve_build_commit(REPO, existing=None, env={"REBAR_BUILD_COMMIT": ""})


def test_wheel_from_sdist_bakes_env_commit(built_artifacts: tuple[Path, Path]) -> None:
    """The core defect: `python -m build` builds the wheel from the extracted sdist (no
    .git); with the fix + REBAR_BUILD_COMMIT set, the WHEEL bakes that exact short SHA
    (today, unfixed, this yields None)."""
    wheel, _sdist = built_artifacts
    assert _wheel_commit(wheel) == "abc1234", "wheel-from-sdist did not bake REBAR_BUILD_COMMIT"


def test_build_fails_when_env_set_but_empty(tmp_path: Path) -> None:
    tree = tmp_path / "src"
    materialize_build_hook_package(REPO, tree)
    out = tmp_path / "dist"
    cp = _build(tree, out, {"REBAR_BUILD_COMMIT": ""})
    assert cp.returncode != 0, "an empty REBAR_BUILD_COMMIT (release context) must fail the build"


def test_sdist_ships_build_info(built_artifacts: tuple[Path, Path]) -> None:
    """The sdist must contain _build_info.py so an install-from-sdist rebuild has a baked
    SHA to preserve (step 2)."""
    import tarfile

    _wheel, sdist = built_artifacts
    with tarfile.open(sdist) as tf:
        assert any(n.endswith("rebar/_build_info.py") for n in tf.getnames()), (
            "sdist does not ship _build_info.py — install-from-sdist would lose provenance"
        )


@pytest.mark.filterwarnings(
    r"error:Python 3\.14 will, by default, filter extracted tar archives.*:DeprecationWarning"
)
def test_install_from_sdist_preserves_commit(
    tmp_path: Path, built_artifacts: tuple[Path, Path]
) -> None:
    """Rebuild a wheel FROM the shipped sdist with NO env var and NO .git — the baked SHA
    the sdist carried must be PRESERVED (step 2), not overwritten with None."""
    _wheel, sdist = built_artifacts
    # Extract the sdist (its _build_info.py pins the commit seeded above) and rebuild the wheel
    # from it with the env var UNSET — the preserve-existing path must keep that pinned value.
    import tarfile

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(sdist) as tf:
        tf.extractall(extracted, filter="data")
    inner = next(extracted.iterdir())
    out2 = tmp_path / "dist2"
    cp = _build(inner, out2, {})  # no REBAR_BUILD_COMMIT, no .git
    assert cp.returncode == 0, f"rebuild-from-sdist failed: {cp.stderr[-2000:]}"
    wheel = next(out2.glob("*.whl"), None)
    assert wheel is not None, "no wheel produced"
    assert _wheel_commit(wheel) == "abc1234", (
        "install-from-sdist lost the baked COMMIT (preserve-existing broken)"
    )
