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

    assert resolved, (
        "no dataset-bearing eval spec resolved under src/rebar/llm/eval_specs/*.eval.yaml — "
        "the disjoint check below would be a fail-open tautology"
    )
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


# --- Gate-finding regression tests (LLM-Review round 1 on Gerrit change 2610) ----------
#
# Each of these targets a finding the code review raised against patch set 1: a blocking
# error-handling gap (a misconfigured live CI run must not exit 0 silently) and five
# coverage/robustness advisories. The behavioural findings (require-live, zero-SHA,
# stdout stream purity) are RED before their fix; the pure coverage ones exercise
# already-correct behaviour that had no test.

from types import SimpleNamespace  # noqa: E402

import rebar._cli._llm_eval_commands as eval_cmds  # noqa: E402
import rebar.llm.evals.changed_criteria as changed_criteria_mod  # noqa: E402


def _fake_report(criterion_id: str) -> dict:
    return {
        "criterion": criterion_id,
        "prompt": f"plan-review-{criterion_id}",
        "n_fire": 1,
        "n_nofire": 1,
        "n_discrimination": 0,
        "runs": 1,
        "recall": 1.0,
        "false_accept": 0.0,
        "agreement": 1.0,
        "kappa": 1.0,
        "stability_min": 1.0,
        "stability_mean": 1.0,
    }


def test_positional_id_and_changed_since_are_mutually_exclusive(tmp_path: Path) -> None:
    """(coverage) `criteria eval <id> --changed-since <ref>` exits 2 and says so."""
    repo = _seed_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    proc = subprocess.run(
        ["rebar", "criteria", "eval", "T8", "--changed-since", base],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2, proc.stderr
    assert "mutually exclusive" in proc.stderr.lower()


def test_bad_changed_since_ref_exits_1_naming_the_ref(tmp_path: Path) -> None:
    """(coverage) A bad --changed-since ref fails git diff → exit 1, ref named on stderr."""
    repo = _seed_repo(tmp_path)
    proc = _run_changed_since(repo, "no-such-ref-xyz")
    assert proc.returncode == 1, proc.stdout
    assert "no-such-ref-xyz" in proc.stderr


def test_rebar_prompts_layout_rubric_shape_is_recognized(tmp_path: Path) -> None:
    """(coverage) A changed `.rebar/prompts/plan-review-*.md` mapping to no registry
    criterion is named on stderr as unmapped — the second `_has_rubric_shape` branch."""
    repo = _seed_repo(tmp_path)
    (repo / ".rebar" / "prompts").mkdir(parents=True)
    base = _git(repo, "rev-parse", "HEAD")
    (repo / ".rebar/prompts/plan-review-bogus.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "new prompt-library rubric")
    proc = _run_changed_since(repo, base)
    assert proc.returncode == 0, proc.stderr
    assert ".rebar/prompts/plan-review-bogus.md" in proc.stderr


def test_first_push_zero_sha_ref_selects_nothing_without_crashing(tmp_path: Path) -> None:
    """(edge-cases) The push all-zero `before` SHA (first push to a new branch) is treated
    as 'no prior ref' — empty selection, exit 0, a stderr note — not a git-diff crash."""
    repo = _seed_repo(tmp_path)
    _touch_rubric(repo, "T8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "T8 rubric on a fresh branch")
    proc = _run_changed_since(repo, "0" * 40)
    assert proc.returncode == 0, proc.stderr
    assert _selection(proc) == []
    assert "first push" in proc.stderr.lower() or "no prior" in proc.stderr.lower()


def test_require_live_without_backend_exits_nonzero(monkeypatch, capsys) -> None:
    """(error-handling, BLOCKING) With --require-live and selected criteria but no LLM
    backend available, the command does NOT silently succeed — it errors and exits non-zero
    so a misconfigured CI job shows red instead of a green no-op."""
    monkeypatch.setattr(eval_cmds, "_live_criteria_eval_available", lambda: False)
    monkeypatch.setattr(eval_cmds, "_changed_since_repo_root", lambda cfg: None)
    monkeypatch.setattr(
        eval_cmds,
        "_changed_paths_since",
        lambda ref, *, cwd: ["src/rebar/llm/reviewers/plan_review_T8.md"],
    )
    monkeypatch.setattr(
        changed_criteria_mod,
        "select_changed_criteria",
        lambda paths, root, **k: changed_criteria_mod.ChangedCriteriaSelection(("T8",), ()),
    )
    args = SimpleNamespace(
        criterion_id=None, changed_since="deadbeef", require_live=True, runs=1, output="text"
    )
    rc = eval_cmds._criteria_eval_changed_since(args)
    captured = capsys.readouterr()
    assert rc != 0
    assert "T8" in captured.out  # the selection is still emitted for auditability
    assert "live" in captured.err.lower() and (
        "unavailable" in captured.err.lower() or "credential" in captured.err.lower()
    )


def test_live_calibration_reports_do_not_pollute_the_selection_stdout(monkeypatch, capsys) -> None:
    """(maintainability) When the live path runs, stdout stays the pure sorted id list; the
    human calibration reports go to stderr so a consumer parsing stdout gets only ids."""
    monkeypatch.setattr(eval_cmds, "_live_criteria_eval_available", lambda: True)
    monkeypatch.setattr(eval_cmds, "_changed_since_repo_root", lambda cfg: None)
    monkeypatch.setattr(
        eval_cmds,
        "_changed_paths_since",
        lambda ref, *, cwd: ["src/rebar/llm/reviewers/plan_review_T8.md"],
    )
    monkeypatch.setattr(
        changed_criteria_mod,
        "select_changed_criteria",
        lambda paths, root, **k: changed_criteria_mod.ChangedCriteriaSelection(("T8",), ()),
    )
    import rebar.llm.evals.eval as eval_engine

    monkeypatch.setattr(
        eval_engine, "calibrate_criterion", lambda cid, *, repo_root, runs: _fake_report(cid)
    )
    args = SimpleNamespace(
        criterion_id=None, changed_since="deadbeef", require_live=False, runs=1, output="text"
    )
    rc = eval_cmds._criteria_eval_changed_since(args)
    captured = capsys.readouterr()
    assert rc == 0
    assert [ln.strip() for ln in captured.out.splitlines() if ln.strip()] == ["T8"]
    assert "Calibration for criterion" in captured.err
