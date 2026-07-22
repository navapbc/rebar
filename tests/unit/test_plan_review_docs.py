"""Documentation contract for advanced plan-review phase operations."""

from pathlib import Path


def test_advanced_phase_contract_sections_are_documented() -> None:
    text = (Path(__file__).parents[2] / "docs/plan-review-gate.md").read_text()
    for heading in (
        "### Phase/floor manifest contract",
        "### `phase_status` compatibility",
        "### Why the execution floor is fixed at 0.80",
        "### `PlanReviewGeneration` signing transaction",
        "### Phase rollback and precision loss",
    ):
        assert heading in text
