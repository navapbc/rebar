"""Enumeration-closure lint (bug c9c2-c569-f5cb-426f), surfaced through advisory P6.

A plan that removes a symbol, or carries a required-empty grep AC, must enumerate every
tree path that carries it; a test module whose module-level import binds a deleted
module loses ALL its tests, not just the one the plan names. Each test builds a small git
repo (or a plain snapshot tree) and runs ``p6_ac_quality`` over it, so the lint is proven
through the same entry point the gate uses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from rebar.llm.plan_review.det_floor import DetResult, PlanContext, p6_ac_quality


def _write_tree(root: Path) -> None:
    files = {
        "src/spot.py": "def fidelity_spot_eval():\n    return 1\n",
        "src/user.py": "from spot import fidelity_spot_eval\n\nVALUE = fidelity_spot_eval()\n",
        "docs/research/sota.md": "The batch-rerun seam is fidelity_spot_eval.\n",
        "tests/test_spot.py": (
            "from spot import fidelity_spot_eval\n\n\n"
            "def test_eval_returns_one():\n    assert fidelity_spot_eval() == 1\n\n\n"
            "def test_shared_setup():\n    assert True\n\n\n"
            "class TestSpot:\n    def test_method_survivor(self):\n        assert True\n"
        ),
        "tests/test_other.py": "def test_unrelated():\n    assert True\n",
    }
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write_tree(root)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def _p6(root: Path, description: str, file_impact: list[str] | None = None) -> DetResult:
    ctx = PlanContext(
        ticket_id="c9c2-c569-f5cb-426f",
        ticket_type="bug",
        title="Retire the spot eval seam",
        description=description,
        state={"file_impact": [{"path": p, "reason": "r"} for p in file_impact or []]},
        repo_root=str(root),
    )
    return p6_ac_quality(ctx)


def _closure_evidence(result: DetResult) -> list[str]:
    evidence = (result.finding or {}).get("evidence", [])
    return [e for e in evidence if e.startswith("enumeration closure")]


_GREP_PLAN = """## Approach
Delete `src/spot.py` and remove the `fidelity_spot_eval` import from `src/user.py`.

## Acceptance Criteria
- [ ] AC1: `git grep -n 'fidelity_spot_eval' -- src docs` returns no hits.
"""


def test_omitted_doc_path_named_for_required_empty_grep(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    evidence = _closure_evidence(_p6(root, _GREP_PLAN))
    assert any("docs/research/sota.md:1" in e for e in evidence), evidence
    assert not any("src/user.py" in e or "src/spot.py" in e for e in evidence), evidence


def test_omitted_path_named_for_removed_symbol_without_grep_ac(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    plan = (
        "## Approach\nRemove `fidelity_spot_eval` from `src/spot.py` and `src/user.py`.\n\n"
        "## Acceptance Criteria\n- [ ] AC1: `pytest tests/test_other.py` passes.\n"
    )
    evidence = _closure_evidence(_p6(root, plan))
    joined = "\n".join(evidence)
    assert "docs/research/sota.md" in joined
    assert "tests/test_spot.py" in joined
    assert "removed symbol `fidelity_spot_eval`" in joined


def test_omitted_path_names_the_matched_text_of_an_over_matching_regex(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    (root / "src/aliases.py").write_text("_ARCH_ALIASES = {}\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    plan = (
        "## Approach\nDelete `_LEGACY_ENV_ALIASES` from `src/spot.py`.\n\n"
        "## Acceptance Criteria\n"
        "- [ ] AC1: `git grep -n '_ALIASES\\b\\|fidelity_spot_eval' -- src` is empty.\n"
    )
    evidence = _closure_evidence(_p6(root, plan, file_impact=["src/user.py"]))
    assert any("src/aliases.py:1" in e and "`_ARCH_ALIASES`" in e for e in evidence), evidence


def test_omitted_path_found_in_a_plain_snapshot_tree(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    _write_tree(root)  # the attested snapshot is a plain tree, not a git checkout
    evidence = _closure_evidence(_p6(root, _GREP_PLAN))
    assert any("docs/research/sota.md" in e for e in evidence), evidence


def test_covered_enumeration_emits_nothing(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    plan = _GREP_PLAN.replace(
        "from `src/user.py`.",
        "from `src/user.py`, drop the mention in `docs/research/` and delete `tests/test_spot.py`.",
    )
    result = _p6(root, plan)
    assert _closure_evidence(result) == []
    assert result.coverage["closure_sources"] >= 1
    assert result.coverage["closure_gaps"] == 0


def test_covered_by_file_impact_emits_nothing(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    plan = (
        "## Acceptance Criteria\n"
        "- [ ] AC1: `git grep -n 'fidelity_spot_eval' -- src docs` returns no hits.\n"
    )
    impact = ["src/spot.py", "src/user.py", "docs/research/sota.md"]
    result = _p6(root, plan, file_impact=impact)
    assert _closure_evidence(result) == []


def test_grep_command_is_parsed_never_executed(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    marker = tmp_path / "executed"
    plan = (
        "## Acceptance Criteria\n"
        f"- [ ] AC1: `git grep -n fidelity_spot_eval -- src; touch {marker}` returns no hits.\n"
    )
    _p6(root, plan, file_impact=["src/spot.py", "src/user.py"])
    assert not marker.exists()


def test_module_deletion_names_remaining_tests_in_bound_test_file(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    plan = (
        "## Approach\nDelete `src/spot.py`. Delete `test_eval_returns_one` in "
        "`tests/test_spot.py`. Update `src/user.py` and `docs/research/sota.md`.\n\n"
        "## Acceptance Criteria\n- [ ] AC1: `pytest tests/test_spot.py` passes.\n"
    )
    evidence = _closure_evidence(_p6(root, plan))
    bound = [e for e in evidence if "imports deleted module `spot`" in e]
    assert len(bound) == 1, evidence
    assert "tests/test_spot.py" in bound[0]
    assert "test_shared_setup" in bound[0]
    assert "TestSpot.test_method_survivor" in bound[0]
    assert "test_eval_returns_one" not in bound[0]


def test_module_deletion_skips_a_test_file_deleted_whole(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    plan = (
        "## Approach\nDelete `src/spot.py` and delete `tests/test_spot.py`. Update "
        "`src/user.py` and `docs/research/sota.md`.\n\n"
        "## Acceptance Criteria\n- [ ] AC1: `pytest tests/test_other.py` passes.\n"
    )
    assert _closure_evidence(_p6(root, plan)) == []


def test_uncompilable_grep_pattern_abstains_and_is_counted(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    plan = "## Acceptance Criteria\n- [ ] AC1: `git grep -n '[unclosed' -- src` returns no hits.\n"
    result = _p6(root, plan)
    assert _closure_evidence(result) == []
    assert result.coverage["closure_abstained"] == 1


def test_covered_when_the_grep_excludes_the_path(tmp_path: Path) -> None:
    root = _git_repo(tmp_path)
    plan = (
        "## Acceptance Criteria\n"
        "- [ ] AC1: `git grep -n 'fidelity_spot_eval' -- . ':!docs' ':(exclude)tests'` "
        "returns no hits.\n"
    )
    result = _p6(root, plan, file_impact=["src/"])
    assert _closure_evidence(result) == []
    assert result.coverage["closure_sources"] == 1
