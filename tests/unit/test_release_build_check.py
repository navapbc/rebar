"""`scripts/release_build_check.py` repeats release.yml's build job.

Bug expansive-aromatic-whippet. A bare `python -m build && twine check` builds with the
newest hatchling, whose
Metadata-Version 2.5 the release-pinned twine rejects, so the local check failed a correct
tree. The script runs the release's own steps instead; these tests hold it to release.yml.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml
from _repo_root import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "release_build_check.py"
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _load():
    spec = importlib.util.spec_from_file_location("release_build_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _release_build_run() -> str:
    steps = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))["jobs"]["build"]["steps"]
    wanted = (
        "Build sdist + wheel (hash-locked, --no-isolation)",
        "Validate metadata (PyPI-readiness)",
    )
    return "\n".join(s["run"] for s in steps if s.get("name") in wanted)


# Each release.yml command, paired with the argv fragment the script must run for it.
_PARITY = [
    (
        "pip install --require-hashes --no-deps -r .github/release-requirements.txt",
        ["pip", "install", "--require-hashes", "--no-deps", "-r"],
    ),
    ("pip freeze", ["pip", "freeze"]),
    ("python scripts/check_build_env_locked.py", ["check_build_env_locked.py", "--lock"]),
    ("python -m build --no-isolation", ["build", "--no-isolation"]),
    ("python -m twine check", ["twine", "check"]),
]


def _contains(argv: list[str], fragment: list[str]) -> bool:
    joined = " ".join(Path(a).name if "/" in a or "\\" in a else a for a in argv)
    return " ".join(fragment) in joined


def test_steps_match_release_build_job_in_order(tmp_path: Path) -> None:
    run = _release_build_run()
    # release.yml feeds the guard `--freeze <(pip freeze)`, so the freeze text sits inside
    # the guard line; order the commands by where each one starts executing.
    guard = run.index("python scripts/check_build_env_locked.py")
    starts = {cmd: (guard if cmd == "pip freeze" else run.index(cmd)) for cmd, _ in _PARITY}
    assert "<(pip freeze)" in run
    positions = [starts[cmd] for cmd, _ in _PARITY]
    assert positions == sorted(positions), "the release.yml order changed; update _PARITY"
    steps = _load().release_build_steps("py", tmp_path)
    assert len(steps) == len(_PARITY)
    for (_, argv, _), (cmd, fragment) in zip(steps, _PARITY, strict=True):
        assert _contains(argv, fragment), f"step {argv} does not run release.yml's `{cmd}`"


def test_stops_at_the_first_failing_step_and_names_it(tmp_path: Path, capsys) -> None:
    mod = _load()
    seen: list[list[str]] = []

    def runner(argv, _stdout):
        seen.append(list(argv))
        return 3 if "build" in argv and "--no-isolation" in argv else 0

    code = mod.main(runner=runner, make_venv=lambda _p: "py")
    assert code == 3
    assert not any("twine" in argv for argv in seen), "twine ran after the build failed"
    assert "build sdist + wheel (--no-isolation)" in capsys.readouterr().err


def test_all_steps_pass(tmp_path: Path, capsys) -> None:
    code = _load().main(runner=lambda _a, _o: 0, make_venv=lambda _p: "py")
    assert code == 0
    assert "OK" in capsys.readouterr().out


def test_makefile_target_runs_the_script() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\nrelease-build-check:" in makefile
    assert "python scripts/release_build_check.py" in makefile
