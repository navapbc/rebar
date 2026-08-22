"""Routing posture for the base-reviewer dimensions `correctness` and `edge-cases` (story 4144).

Both are dimensions the base reviewer always evaluates (its evidence-record contract names
them) rather than members of the closed `OVERLAY_IDS` escalation vocabulary. Until this story
neither had a routing entry, so every finding tagged with them resolved through the
unknown-criterion default (0.95, not blocking) — invisible in the routing file and impossible
to tune. These tests pin, corpus-free:

  * that both ids are routed explicitly and are NOT overlays;
  * that the friction budget the routing claims is the one the calibration document records
    — the numeric bound, not merely that the two files name the same ids;
  * that the project overlay cannot clobber a packaged posture;
  * that the replay script's advertised blocking set is the routed one.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

from rebar.llm.code_review import registry

REPO_ROOT = Path(__file__).resolve().parents[2]
CALIBRATION_DOC = REPO_ROOT / "docs" / "experiments" / "code-review-threshold-calibration.md"
REPLAY_SCRIPT = REPO_ROOT / "docs" / "experiments" / "replay_code_v4_would_block.py"

# The heading whose table records this story's measurement. The table is the operator-facing
# record of the friction the routing claims; the routing is the machine-facing posture. They
# are two independent artifacts and the tests below hold them to each other.
REPLAY_HEADING = "## Code-v4 friction replay at 0.54 (correctness, edge-cases, concurrency)"

# The operator-accepted friction ceiling, recorded on the `tests` entry's `_comment`
# ("blocks 99 changes / 8.0%, operator-accepted").
FRICTION_BUDGET_PCT = 8.0

BASE_DIMENSIONS = ("correctness", "edge-cases")


def _routing() -> dict[str, Any]:
    return registry.routing_index()


def _replay_module():
    spec = importlib.util.spec_from_file_location("_replay_code_v4", REPLAY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _measured_rows() -> list[dict[str, str]]:
    """Parse the story's block-impact table out of the calibration document.

    Returns one dict per criterion row, keyed by the table's own column headers."""
    text = CALIBRATION_DOC.read_text(encoding="utf-8")
    assert REPLAY_HEADING in text, f"calibration doc is missing the section {REPLAY_HEADING!r}"
    section = text.split(REPLAY_HEADING, 1)[1].split("\n## ", 1)[0]
    lines = [ln.strip() for ln in section.splitlines() if ln.strip().startswith("|")]
    assert len(lines) >= 3, "expected a markdown table (header, separator, >=1 row)"
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = []
    for line in lines[2:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells, strict=True)))
    assert rows, "the section's table has no data rows"
    return rows


def _pct(cell: str) -> float:
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*%", cell)
    assert m is not None, f"expected a percentage cell, got {cell!r}"
    return float(m.group(1))


@pytest.mark.parametrize("criterion", BASE_DIMENSIONS)
def test_base_dimension_is_routed_explicitly_and_is_not_an_overlay(criterion: str) -> None:
    """The dimension has its own routing entry instead of falling to the unknown default."""
    entry = _routing().get(criterion)
    assert entry is not None, f"{criterion} has no routing entry"
    assert criterion not in registry.OVERLAY_IDS, (
        f"{criterion} is a base-reviewer dimension, not an escalation overlay"
    )
    # An explicit entry is only meaningful if it actually carries a posture.
    assert "blocking_enabled" in entry and "block_threshold" in entry


@pytest.mark.parametrize("criterion", BASE_DIMENSIONS)
def test_routed_dimension_joins_the_canonical_vocabulary(criterion: str) -> None:
    """The gate's canonical vocabulary IS the routing key set, so routing widens it."""
    assert criterion in registry.effective_criteria(str(REPO_ROOT))


@pytest.mark.parametrize("criterion", BASE_DIMENSIONS)
def test_routed_dimension_resolves_to_its_own_posture(criterion: str) -> None:
    """`threshold_for` must read the entry, not the 0.95/advisory unknown-criterion default."""
    entry = _routing()[criterion]
    threshold, blocking = registry.threshold_for([criterion])
    assert threshold == pytest.approx(float(entry["block_threshold"]))
    assert blocking is bool(entry["blocking_enabled"])


def test_every_blocking_row_stays_inside_the_friction_budget() -> None:
    """A criterion the document lists may only be routed blocking if its MEASURED share of
    all changes is at or under the operator-accepted budget. This is the numeric bound, not a
    name-agreement check: a row measured above the budget fails even if the two files agree
    on which ids are blocking."""
    routing = _routing()
    checked = 0
    for row in _measured_rows():
        criterion = row["criterion"].strip("`")
        entry = routing.get(criterion)
        assert entry is not None, f"{criterion} is measured in the doc but has no routing entry"
        share = _pct(row["of all changes"])
        if bool(entry["blocking_enabled"]):
            assert share <= FRICTION_BUDGET_PCT, (
                f"{criterion} is routed blocking but the calibration document records "
                f"{share}% of all changes, above the {FRICTION_BUDGET_PCT}% budget"
            )
            checked += 1
    assert checked, "no measured criterion is routed blocking — the budget check was vacuous"


def test_document_decision_column_matches_the_routed_posture() -> None:
    """`flip` rows must be routed blocking at the threshold they were measured at; `hold`
    rows must stay advisory."""
    routing = _routing()
    decisions = set()
    for row in _measured_rows():
        criterion = row["criterion"].strip("`")
        decision = row["decision"].strip().lower()
        decisions.add(decision)
        entry = routing[criterion]
        if decision == "flip":
            assert entry["blocking_enabled"] is True, f"{criterion} is marked flip but is advisory"
            assert float(entry["block_threshold"]) == pytest.approx(float(row["thr"])), (
                f"{criterion} was measured at {row['thr']} but is routed at "
                f"{entry['block_threshold']}"
            )
        elif decision == "hold":
            assert entry["blocking_enabled"] is False, f"{criterion} is marked hold but blocks"
        else:  # pragma: no cover — an unknown decision word is a document error
            pytest.fail(f"unknown decision {decision!r} for {criterion}")
    assert {"flip", "hold"} <= decisions, "the table should record both a flip and a hold"


def test_project_overlay_cannot_clobber_the_packaged_concurrency_posture() -> None:
    """This repo's `.rebar/criteria_routing.json` re-tunes `concurrency` (trigger tokens and
    globs only). A re-tune merges field-by-field over the packaged entry, so the packaged
    posture must survive it."""
    packaged = _routing()["concurrency"]
    effective = registry.effective_routing(str(REPO_ROOT))["concurrency"]
    assert effective["blocking_enabled"] == packaged["blocking_enabled"]
    assert float(effective["block_threshold"]) == pytest.approx(float(packaged["block_threshold"]))
    # The re-tune itself still applies — otherwise this test would pass on a broken overlay.
    assert effective.get("trigger_tokens"), "the project re-tune did not merge"


def test_replay_script_advertises_the_routed_blocking_set() -> None:
    """The replay's printed summary must name the criteria whose packaged posture can block —
    the set its own gate applies via `threshold_for` — so the two cannot drift."""
    advertised = set(_replay_module().blocking_set())
    routing = _routing()
    for criterion, entry in routing.items():
        assert (criterion in advertised) is bool(entry["blocking_enabled"]), (
            f"{criterion}: advertised={criterion in advertised} "
            f"routed blocking={entry['blocking_enabled']}"
        )
    assert {"correctness", "edge-cases", "tests", "regression"} <= advertised
    assert advertised.isdisjoint({"docs", "concurrency", "scope-intent", "maintainability"})


def test_project_criteria_routing_json_stays_valid() -> None:
    """The packaged routing file must remain parseable JSON with a leading `_comment`."""
    raw = json.loads(
        (Path(registry.__file__).parent / "criteria_routing.json").read_text(encoding="utf-8")
    )
    assert raw["_comment"]
    for criterion in BASE_DIMENSIONS:
        assert raw[criterion]["_comment"], f"{criterion} must record why it is routed"


def _calibrate_module():
    spec = importlib.util.spec_from_file_location(
        "_calibrate_code_review",
        REPO_ROOT / "docs" / "experiments" / "calibrate_code_review_thresholds.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _synthetic_corpus() -> dict[str, list[dict[str, Any]]]:
    """Two changes' worth of pooled findings with known priorities and validities.

    `alpha` carries two `correctness` findings at/above 0.54 (so the change is hit once, not
    twice) and one below; `beta` carries one below-threshold `correctness` finding, so it is
    NOT hit. A `dropped`-pool finding is present to prove only surviving pools are counted."""

    def f(criteria, priority, validity):
        return {"criteria": list(criteria), "priority": priority, "validity": validity}

    return {
        "alpha": [
            {
                "pools": {
                    "blocking": [f(["correctness"], 0.9, 1.0)],
                    "advisory": [f(["correctness"], 0.54, 0.8), f(["correctness"], 0.3, 1.0)],
                    "dropped": [f(["correctness"], 0.99, 1.0)],
                    "indeterminate": [],
                }
            }
        ],
        "beta": [
            {
                "pools": {
                    "blocking": [],
                    "advisory": [f(["correctness"], 0.53, 1.0), f(["edge-cases"], 0.9, 1.0)],
                    "dropped": [],
                    "indeterminate": [],
                }
            }
        ],
    }


def test_block_impact_columns_are_computed_from_the_surviving_pools() -> None:
    """The `--block-impact` arithmetic, pinned without the (uncommitted) corpus."""
    result = _calibrate_module().block_impact(_synthetic_corpus(), "correctness", 0.54)
    assert result["surviving"] == 4  # 3 in alpha + 1 in beta; the dropped one is excluded
    assert result["would_block"] == 2  # 0.9 and 0.54; 0.3 and 0.53 fall below
    assert result["changes_hit"] == 1  # both hits are the same change
    assert result["of_all_changes"] == pytest.approx(50.0)  # 1 of 2 changes
    assert result["of_surviving"] == pytest.approx(50.0)  # 2 of 4 surviving
    assert result["mean_validity"] == pytest.approx(0.9)  # mean(1.0, 0.8)
    assert result["val_lt_half"] == 0


def test_block_impact_on_an_empty_corpus_reports_zeroes_instead_of_dividing_by_zero() -> None:
    result = _calibrate_module().block_impact({}, "correctness", 0.54)
    assert result["surviving"] == 0
    assert result["of_all_changes"] == 0.0
    assert result["of_surviving"] == 0.0
    assert result["mean_validity"] is None
