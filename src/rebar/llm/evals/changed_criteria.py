"""Select plan-review criterion evals whose rubric files changed."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class ChangedCriteriaSelection:
    """Criterion ids selected by changed rubrics, plus unmapped rubric-shaped paths."""

    selected: tuple[str, ...]
    unmapped: tuple[str, ...]


def select_changed_criteria(
    changed_paths: Iterable[str],
    repo_root: str | Path,
    *,
    gate_key: str = "plan_review",
) -> ChangedCriteriaSelection:
    """Map changed repo-relative rubric paths to effective criterion ids."""

    rubric_to_criterion = _rubric_to_criterion(repo_root, gate_key=gate_key)
    selected: set[str] = set()
    unmapped: set[str] = set()

    for raw_path in changed_paths:
        changed_path = raw_path.strip()
        if not changed_path:
            continue
        criterion_id = rubric_to_criterion.get(changed_path)
        if criterion_id is not None:
            selected.add(criterion_id)
        elif _has_rubric_shape(changed_path):
            unmapped.add(changed_path)

    return ChangedCriteriaSelection(tuple(sorted(selected)), tuple(sorted(unmapped)))


def _rubric_to_criterion(repo_root: str | Path, *, gate_key: str) -> dict[str, str]:
    from rebar.llm.criteria import effective_routing
    from rebar.llm.evals.fixture_selection import rubric_path

    root = Path(repo_root)
    rubric_map: dict[str, str] = {}
    for criterion_id in effective_routing(str(root), gate_key=gate_key):
        try:
            rubric = rubric_path(criterion_id, repo_root=str(root), gate_key=gate_key)
        except FileNotFoundError:
            continue
        try:
            repo_relative = rubric.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        rubric_map[repo_relative] = criterion_id
    return rubric_map


def _has_rubric_shape(path: str) -> bool:
    posix = PurePosixPath(path)
    return (
        path.startswith("src/rebar/llm/reviewers/")
        and posix.name.startswith("plan_review_")
        and posix.name.endswith(".md")
    ) or (
        posix.parent.as_posix() == ".rebar/prompts"
        and posix.name.startswith("plan-review-")
        and posix.name.endswith(".md")
    )
