"""Held-out oracle for story 5169 (modern-loathful-snake): run a criterion's fixtures
when its rubric changes, keeping them off the weekly live sweep.

Observable-behaviour tests only — the CLI's stdout selection, stderr warnings, exit codes,
and the resolved weekly-sweep spec list on disk. Nothing here asserts a private name or
internal structure, so a behaviour-preserving refactor of the engine cannot break them.

`rebar criteria eval --changed-since <ref>` is SELECTION-ONLY offline: it prints the
registry criterion ids whose rubric file changed in the range (sorted, one per line),
warns on stderr for a changed rubric-shaped path that maps to no registry criterion, and
exits 0 without a model call. (The workflow job supplies credentials and runs each selected
criterion live; that live path needs a model and is out of this offline oracle's scope.)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
REVIEWERS = "src/rebar/llm/reviewers"

# Criterion ids this epic covers, and the prompt-library ids their rubrics carry. AC4 pins
# that none of these prompt ids ride the weekly cron.
EPIC_CRITERIA = ("T8", "G6", "G3", "G4")
EPIC_PROMPT_IDS = tuple(f"plan-review-{c}" for c in EPIC_CRITERIA)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _seed_repo(tmp: Path) -> Path:
    """A git repo with an initial commit and the reviewers dir present."""
    repo = tmp / "repo"
    (repo / REVIEWERS).mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "d@e.test")
    _git(repo, "config", "user.name", "D")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _touch_rubric(repo: Path, criterion: str, body: str = "rubric\n") -> None:
    (repo / REVIEWERS / f"plan_review_{criterion}.md").write_text(body, encoding="utf-8")


def _run_changed_since(
    repo: Path, ref: str, *, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["rebar", "criteria", "eval", "--changed-since", ref],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _selection(proc: subprocess.CompletedProcess) -> list[str]:
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


# --- AC1 (HAPPY PATH — given to the implementer) ---------------------------------------


def test_changed_range_selects_touched_criteria(tmp_path: Path) -> None:
    """A range touching plan_review_T8.md and plan_review_G6.md selects exactly T8 and G6."""
    repo = _seed_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _touch_rubric(repo, "T8")
    _touch_rubric(repo, "G6")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "edit T8 + G6 rubrics")

    proc = _run_changed_since(repo, base)

    assert proc.returncode == 0, proc.stderr
    assert _selection(proc) == ["G6", "T8"]


# --- AC2 (HELD OUT) --------------------------------------------------------------------


def test_no_rubric_change_selects_nothing_and_makes_no_model_call(tmp_path: Path) -> None:
    """A range touching no rubric file selects nothing, exits 0, and issues no model call.

    With no agents extra / credentials configured, a live run would fail loudly; an empty
    selection is the only way exit 0 with empty stdout is reached, which is the observable
    proxy for 'no model call'."""
    repo = _seed_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("no rubric here\n", encoding="utf-8")
    (repo / "src" / "rebar" / "llm" / "evals").mkdir(parents=True, exist_ok=True)
    (repo / "src/rebar/llm/evals/changed_criteria.py").write_text("# code, not a rubric\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "non-rubric edits")

    # Clear anything that could configure a live model.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_REGION", "REBAR_LLM_MODEL")
    }

    proc = _run_changed_since(repo, base, env=env)

    assert proc.returncode == 0, proc.stderr
    assert _selection(proc) == []


# --- AC3 (HELD OUT) --------------------------------------------------------------------


def test_unmapped_rubric_path_named_on_stderr(tmp_path: Path) -> None:
    """A rubric-shaped path matching no registry criterion is named on stderr, not skipped
    silently, and is absent from the selection."""
    repo = _seed_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _touch_rubric(repo, "bogus")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "edit a bogus rubric")

    proc = _run_changed_since(repo, base)

    assert proc.returncode == 0, proc.stderr
    assert "plan_review_bogus.md" in proc.stderr
    assert "bogus" not in _selection(proc)


def test_unmapped_named_but_real_criteria_still_selected(tmp_path: Path) -> None:
    """A bogus rubric warns yet does not suppress a real criterion changed in the same range."""
    repo = _seed_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _touch_rubric(repo, "bogus")
    _touch_rubric(repo, "T8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bogus + T8")

    proc = _run_changed_since(repo, base)

    assert proc.returncode == 0, proc.stderr
    assert _selection(proc) == ["T8"]
    assert "plan_review_bogus.md" in proc.stderr


# --- AC4 (HELD OUT) --------------------------------------------------------------------


def test_epic_prompt_ids_absent_from_weekly_sweep() -> None:
    """The epic's prompt ids are absent from the weekly sweep's resolved (dataset-bearing)
    spec list under src/rebar/llm/eval_specs/*.eval.yaml."""
    import glob

    import yaml

    resolved: set[str] = set()
    for p in glob.glob(str(REPO_ROOT / "src/rebar/llm/eval_specs/*.eval.yaml")):
        spec = yaml.safe_load(Path(p).read_text(encoding="utf-8"))
        if spec.get("dataset"):
            resolved.add(spec.get("prompt"))

    assert resolved.isdisjoint(EPIC_PROMPT_IDS), (
        f"epic prompt ids leaked into the weekly sweep: {resolved & set(EPIC_PROMPT_IDS)}"
    )


# --- AC5 (HELD OUT) --------------------------------------------------------------------


def test_selection_produced_with_cleared_ci_environment(tmp_path: Path) -> None:
    """The command produces a selection with no CI environment variables set."""
    repo = _seed_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _touch_rubric(repo, "T8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "T8 rubric")

    ci_vars = (
        "CI",
        "GITHUB_ACTIONS",
        "GITHUB_REF",
        "GITHUB_SHA",
        "GITHUB_EVENT_NAME",
        "GITHUB_BASE_REF",
        "GITHUB_HEAD_REF",
        "RUNNER_TEMP",
    )
    env = {k: v for k, v in os.environ.items() if k not in ci_vars}

    proc = _run_changed_since(repo, base, env=env)

    assert proc.returncode == 0, proc.stderr
    assert _selection(proc) == ["T8"]


# --- AC6 (HELD OUT) --------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not on PATH")
def test_prompt_eval_workflow_passes_actionlint() -> None:
    """The prompt-eval workflow (carrying the new change-triggered job) passes actionlint."""
    wf = REPO_ROOT / ".github/workflows/prompt-eval.yml"
    proc = subprocess.run(["actionlint", str(wf)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
