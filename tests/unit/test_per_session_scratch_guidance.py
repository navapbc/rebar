from __future__ import annotations

import re
import subprocess
from pathlib import Path

from _subprocess_env import subprocess_env

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "AGENTS.md"
ADVISORY_CANARY = ROOT / ".github" / "workflows" / "dependency-advisory-canary.yml"
DIAG_MATRIX = ROOT / "docs" / "experiments" / "diag_runaway_matrix.py"
FIXED_TMP_REDIRECT = re.compile(r">\s*/tmp/[A-Za-z0-9_.-]+")


def test_agents_guidance_prescribes_per_session_scratch_example() -> None:
    text = AGENTS.read_text(encoding="utf-8")

    assert "## Use per-session scratch paths for evidence" in text
    assert 'scratch_dir="${PWD}/.rebar/scratch/${REBAR_SESSION_ID:-manual}-$$"' in text
    assert "Never redirect logs or evidence to a fixed `/tmp/<name>` path" in text
    assert "overwrites or interleaves another session's evidence" in text


def test_dependency_advisory_canary_uses_unique_push_error_path() -> None:
    text = ADVISORY_CANARY.read_text(encoding="utf-8")

    assert (
        'push_err="${RUNNER_TEMP:-.}/push_err-${GITHUB_RUN_ID:-manual}-${GITHUB_RUN_ATTEMPT:-0}-$$"'
    ) in text
    assert "trap 'rm -f \"$push_err\"' EXIT" in text
    assert '2>"$push_err"' in text
    assert "/tmp/push_err" not in text


def test_push_error_template_is_distinct_per_process(tmp_path: Path) -> None:
    script = (
        "set -euo pipefail\n"
        'push_err="${RUNNER_TEMP:-.}/push_err-${GITHUB_RUN_ID:-manual}-${GITHUB_RUN_ATTEMPT:-0}-$$"\n'
        'mkdir -p "$(dirname "$push_err")"\n'
        'printf "%s\\n" "$push_err"\n'
    )
    env = subprocess_env(
        {
            "PATH": "/bin:/usr/bin",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_RUN_ID": "same-run",
            "GITHUB_RUN_ATTEMPT": "1",
        }
    )

    first = subprocess.run(
        ["bash", "-c", script], check=True, env=env, text=True, capture_output=True
    )
    second = subprocess.run(
        ["bash", "-c", script], check=True, env=env, text=True, capture_output=True
    )

    first_path = Path(first.stdout.strip())
    second_path = Path(second.stdout.strip())
    assert first_path != second_path
    assert first_path.parent == tmp_path
    assert second_path.parent == tmp_path


def test_repo_guidance_has_no_fixed_tmp_redirects_outside_infra() -> None:
    roots = [
        ROOT / "AGENTS.md",
        ROOT / "docs",
        ROOT / "scripts",
        ROOT / ".github",
        ROOT / "src",
        ROOT / "tests",
    ]
    matches: list[str] = []
    for root in roots:
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if "node_modules" in path.parts:
                continue
            if path.is_file() and path.suffix in {".md", ".py", ".sh", ".yml", ".yaml"}:
                for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if FIXED_TMP_REDIRECT.search(line):
                        matches.append(f"{path.relative_to(ROOT)}:{lineno}:{line.strip()}")

    assert matches == []


def test_diag_runaway_matrix_default_output_is_unique_per_process() -> None:
    text = DIAG_MATRIX.read_text(encoding="utf-8")

    assert 'f".rebar/scratch/diag_matrix-{os.getpid()}.csv"' in text
    assert 'os.environ.get("DIAG_MATRIX_PATH"' in text
    assert "/tmp/diag_matrix.csv" not in text
