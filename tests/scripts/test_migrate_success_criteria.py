"""Happy-path contract for the Success Criteria store migration (ticket 0a8c).

Tier: unit (pure text rewrite — no store, no git). Pins the core: a ticket carrying
a `## Success Criteria` block has its criteria moved under `## Acceptance Criteria`
as `- [ ]` checklist items, so they finally reach the check-ac blocking floor.

The script exposes `rewrite_description(description) -> str`, returning the migrated
description and returning the input UNCHANGED when there is no SC block.

Block shapes beyond the two below, collateral invariants, idempotency and the
write path are held out.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_success_criteria.py"


def _load():
    spec = importlib.util.spec_from_file_location("migrate_success_criteria", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_success_criteria_only_becomes_an_acceptance_criteria_checklist() -> None:
    """SC is the ticket's only criteria block: rename it and check-box its bullets."""
    out = _load().rewrite_description(
        "## Why\nbecause.\n\n## Success Criteria\n- the thing ships\n- the other thing ships\n"
    )

    assert "## Success Criteria" not in out
    assert "## Acceptance Criteria" in out
    assert "- [ ] the thing ships" in out
    assert "- [ ] the other thing ships" in out
    assert "## Why\nbecause." in out


def test_success_criteria_merges_into_an_existing_acceptance_criteria_block() -> None:
    """Both headings present: SC's items join the existing AC list, SC heading goes."""
    out = _load().rewrite_description(
        "## Acceptance Criteria\n- [ ] already here\n\n## Success Criteria\n- the thing ships\n"
    )

    assert "## Success Criteria" not in out
    assert out.count("## Acceptance Criteria") == 1
    assert "- [ ] already here" in out
    assert "- [ ] the thing ships" in out
