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

import rebar

REPO = Path(rebar.__file__).resolve().parents[2]


def _clean_tree(dest: Path) -> Path:
    """A clean copy of the repo build inputs with NO .git (mirrors the sdist-extract tree
    the wheel is built from). Uses `git archive` so it is fast and ignores untracked cruft."""
    dest.mkdir(parents=True, exist_ok=True)
    tar = subprocess.run(
        ["git", "-C", str(REPO), "archive", "HEAD"], capture_output=True, check=True
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=tar, check=True)
    return dest


def _build(tree: Path, outdir: Path, env_extra: dict) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env.pop("REBAR_BUILD_COMMIT", None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(outdir), str(tree)],
        capture_output=True,
        text=True,
        env=env,
    )


def _wheel_commit(outdir: Path) -> str | None:
    wheels = list(outdir.glob("*.whl"))
    assert wheels, "no wheel produced"
    with zipfile.ZipFile(wheels[0]) as zf:
        name = next(n for n in zf.namelist() if n.endswith("rebar/_build_info.py"))
        ns: dict = {}
        exec(zf.read(name).decode(), ns)  # noqa: S102 - reading our own generated module
        return ns.get("COMMIT")


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


def test_wheel_from_sdist_bakes_env_commit(tmp_path: Path) -> None:
    """The core defect: `python -m build` builds the wheel from the extracted sdist (no
    .git); with the fix + REBAR_BUILD_COMMIT set, the WHEEL bakes that exact short SHA
    (today, unfixed, this yields None)."""
    tree = _clean_tree(tmp_path / "src")
    out = tmp_path / "dist"
    cp = _build(tree, out, {"REBAR_BUILD_COMMIT": "abc1234"})
    assert cp.returncode == 0, f"build failed: {cp.stderr[-2000:]}"
    assert _wheel_commit(out) == "abc1234", "wheel-from-sdist did not bake REBAR_BUILD_COMMIT"


def test_build_fails_when_env_set_but_empty(tmp_path: Path) -> None:
    tree = _clean_tree(tmp_path / "src")
    out = tmp_path / "dist"
    cp = _build(tree, out, {"REBAR_BUILD_COMMIT": ""})
    assert cp.returncode != 0, "an empty REBAR_BUILD_COMMIT (release context) must fail the build"


def test_sdist_ships_build_info(tmp_path: Path) -> None:
    """The sdist must contain _build_info.py so an install-from-sdist rebuild has a baked
    SHA to preserve (step 2)."""
    import tarfile

    tree = _clean_tree(tmp_path / "src")
    out = tmp_path / "dist"
    cp = _build(tree, out, {"REBAR_BUILD_COMMIT": "abc1234"})
    assert cp.returncode == 0, f"build failed: {cp.stderr[-2000:]}"
    sdists = list(out.glob("*.tar.gz"))
    assert sdists, "no sdist produced"
    with tarfile.open(sdists[0]) as tf:
        assert any(n.endswith("rebar/_build_info.py") for n in tf.getnames()), (
            "sdist does not ship _build_info.py — install-from-sdist would lose provenance"
        )


def test_install_from_sdist_preserves_commit(tmp_path: Path) -> None:
    """Rebuild a wheel FROM the shipped sdist with NO env var and NO .git — the baked SHA
    the sdist carried must be PRESERVED (step 2), not overwritten with None."""
    tree = _clean_tree(tmp_path / "src")
    out1 = tmp_path / "dist1"
    assert _build(tree, out1, {"REBAR_BUILD_COMMIT": "abc1234"}).returncode == 0
    sdist = next(out1.glob("*.tar.gz"))
    # Extract the sdist (it carries _build_info.py with COMMIT=abc1234) and rebuild the wheel
    # from it with the env var UNSET — the preserve-existing path must keep abc1234.
    import tarfile

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(sdist) as tf:
        tf.extractall(extracted)
    inner = next(extracted.iterdir())
    out2 = tmp_path / "dist2"
    cp = _build(inner, out2, {})  # no REBAR_BUILD_COMMIT, no .git
    assert cp.returncode == 0, f"rebuild-from-sdist failed: {cp.stderr[-2000:]}"
    assert _wheel_commit(out2) == "abc1234", (
        "install-from-sdist lost the baked COMMIT (preserve-existing broken)"
    )
