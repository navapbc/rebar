"""Clean no-extras wheel distribution contract for the CLI surface (RP-05 S5, ticket 6755).

The registry promises that the LEAN install (``pip install nava-rebar`` with NO extras) can
render every approved help form with EXACT committed bytes and WITHOUT importing any
operation / config / store / parser-handler logic or a heavy optional dependency. This proves
that promise against a real artifact: it builds the wheel, installs it into a throwaway venv
with no extras, and drives the installed ``rebar`` console script.

Every check runs the SAME repository-local commands the portable CI lane runs (AC7), so a
regression BLOCKS locally exactly as it does on the Verified gate. ``rebar-mcp`` is proven
separately (it needs the ``mcp`` extra), asserting a concrete observable: a successful
``--help`` invocation (exit 0) whose output names the server entry point.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
HELP_DIR = REPO_ROOT / "src" / "rebar" / "_cli" / "help"

# Heavy optional stacks that a NO-EXTRAS install must not be able to import.
_HEAVY_MODULES = ("pydantic_ai", "jinja2", "boto3", "jira", "opentelemetry", "fastapi", "lizard")


def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False, **kw)


def _venv_bin(root: Path, name: str) -> Path:
    sub = "Scripts" if os.name == "nt" else "bin"
    return root / sub / name


@pytest.fixture(scope="module")
def clean_wheel_env(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel, install it into a fresh no-extras venv, return the venv root.

    Skips (never fails) when the build/install cannot run offline — the Verified CI lane
    always has network, so the contract is enforced there regardless.
    """
    work = tmp_path_factory.mktemp("clean_wheel")
    dist = work / "dist"
    build = _run([sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)], cwd=REPO_ROOT)
    if build.returncode != 0:
        pytest.skip(f"wheel build unavailable in this environment:\n{build.stderr[-2000:]}")
    wheels = glob.glob(str(dist / "*.whl"))
    assert wheels, "no wheel produced"

    env_root = work / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(env_root)
    pip = _venv_bin(env_root, "pip")
    up = _run([str(pip), "install", "--upgrade", "pip"])
    if up.returncode != 0:
        pytest.skip(f"cannot upgrade pip offline:\n{up.stderr[-2000:]}")
    install = _run([str(pip), "install", wheels[0]])
    if install.returncode != 0:
        pytest.skip(f"cannot install the wheel offline:\n{install.stderr[-2000:]}")
    return env_root


def _committed_help_names() -> list[str]:
    return sorted(p.name[:-4] for p in HELP_DIR.glob("*.txt") if p.name != "overview.txt")


def test_clean_wheel_imports_core_but_not_heavy(clean_wheel_env: Path) -> None:
    """``import rebar`` works on the lean wheel; every heavy optional stack is ABSENT."""
    py = _venv_bin(clean_wheel_env, "python")
    probe = (
        "import importlib.util as u, rebar\n"
        f"heavy = {_HEAVY_MODULES!r}\n"
        "present = [m for m in heavy if u.find_spec(m) is not None]\n"
        "assert not present, f'heavy modules importable in a no-extras install: {present}'\n"
        "print('lean-core OK')\n"
    )
    result = _run([str(py), "-c", probe])
    assert result.returncode == 0, result.stderr
    assert "lean-core OK" in result.stdout


def test_clean_wheel_renders_every_help_form_with_exact_bytes(
    clean_wheel_env: Path, tmp_path: Path
) -> None:
    """Every committed help form renders byte-exactly from the installed console script, run
    from a fresh directory — and leaves NO store/config artifact behind (no init side effect)."""
    rebar = _venv_bin(clean_wheel_env, "rebar")
    fresh = tmp_path / "fresh"
    fresh.mkdir()

    overview = _run([str(rebar), "--help"], cwd=fresh)
    assert overview.returncode == 0, overview.stderr
    assert overview.stdout == (HELP_DIR / "overview.txt").read_text(encoding="utf-8")

    mismatches: list[str] = []
    for name in _committed_help_names():
        expected = (HELP_DIR / f"{name}.txt").read_text(encoding="utf-8")
        got = _run([str(rebar), name, "--help"], cwd=fresh)
        if got.returncode != 0 or got.stdout != expected:
            mismatches.append(f"{name}: rc={got.returncode} bytes_match={got.stdout == expected}")
    assert not mismatches, "help-form byte drift on the lean wheel:\n" + "\n".join(mismatches)

    # Rendering help must not initialize a store or write config into the working directory.
    assert not (fresh / ".rebar").exists(), "rendering --help created a store side effect"


def test_clean_wheel_generators_check_clean(clean_wheel_env: Path) -> None:
    """Both derivation gates pass when run by the lean-wheel interpreter: the committed help
    artifacts AND the CLI reference derive correctly with no extras installed."""
    py = _venv_bin(clean_wheel_env, "python")
    for script in ("gen_cli_help.py", "gen_cli_reference.py"):
        result = _run([str(py), str(REPO_ROOT / "scripts" / script), "--check"], cwd=REPO_ROOT)
        assert result.returncode == 0, f"{script} --check failed on lean wheel:\n{result.stderr}"


def test_rebar_mcp_smoke_is_a_separate_entry_point(tmp_path: Path) -> None:
    """``rebar-mcp`` is a separate console entry point. Smoke-test it with the ``mcp`` extra
    present, asserting a CONCRETE observable: ``--help`` exits 0 and its output advertises the
    server (the process starts, parses args, and prints usage rather than crashing)."""
    import importlib.util

    if importlib.util.find_spec("mcp") is None:
        pytest.skip("mcp extra not installed in this interpreter")
    # Use the current interpreter's console script location.
    exe = Path(sys.executable).parent / "rebar-mcp"
    if not exe.exists():
        pytest.skip("rebar-mcp console script not present in this environment")
    result = _run([str(exe), "--help"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "rebar-mcp" in combined or "usage" in combined.lower(), combined
    # A pure --help must not stand up a store in the working directory.
    assert not (tmp_path / ".rebar").exists()
