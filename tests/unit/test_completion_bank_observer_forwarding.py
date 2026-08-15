"""Regression for the completion-banking observation stub's kwarg forwarding (bug 9c7c).

The live bedrock arm of ``tests/external/test_completion_banking_behavior_0707.py`` failed
with ``observed_upsert() got an unexpected keyword argument 'evidence_sufficient'`` because
the monkeypatch stub for ``CriterionBank.upsert`` had drifted from the real signature: the
real ``upsert`` grew keyword-only ``evidence_sufficient`` / ``seeded`` markers that production
passes on the bounded-fallback and cache-seed paths, but the stub only accepted ``source``.

These tests drive a real :class:`CriterionBank` through the shared stub factory
(:func:`tests._bank_observer.make_observed_upsert`) on exactly the paths that pass those
kwargs, so a stub that fails to forward them raises the same ``TypeError`` here — no live
provider required.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import _bank_observer  # noqa: E402

from rebar.llm.workflow.completion_banking import BankStamps, CriterionBank  # noqa: E402


def _bank(tmp_path: Path) -> CriterionBank:
    stamps = BankStamps(ticket_id="t-9c7c", material_fingerprint=None, tree_sha=None)
    return CriterionBank(tmp_path / "bank", stamps)


def _wrap(bank: CriterionBank):
    writes: list[str] = []
    calls_at_first_write: list[int] = []
    observed = _bank_observer.make_observed_upsert(
        CriterionBank.upsert, writes, calls_at_first_write, lambda: 0
    )
    return observed, writes


def test_observed_upsert_forwards_evidence_sufficient(tmp_path: Path) -> None:
    """record_insufficient() -> upsert(..., evidence_sufficient=False) must survive the stub.

    This is the exact production path (completion_tool_policy -> record_insufficient) the
    bedrock arm hit; before the fix it raised the unexpected-kwarg TypeError.
    """
    bank = _bank(tmp_path)
    observed, _ = _wrap(bank)
    entry = observed(
        bank, "C1", False, "no evidence found", source="fallback", evidence_sufficient=False
    )
    assert entry["evidence_sufficient"] is False
    assert entry["met"] is False


def test_observed_upsert_forwards_seeded(tmp_path: Path) -> None:
    """The cache-seed path passes seeded=True; the stub must forward it too."""
    bank = _bank(tmp_path)
    observed, _ = _wrap(bank)
    entry = observed(bank, "C2", True, "cached pass", source="cache", seeded=True)
    assert entry["seeded"] is True


def test_observed_upsert_records_first_tool_write(tmp_path: Path) -> None:
    """The bookkeeping still fires: the first `tool`-sourced write of a criterion is logged."""
    bank = _bank(tmp_path)
    writes: list[str] = []
    calls_at_first_write: list[int] = []
    counter = {"n": 0}
    observed = _bank_observer.make_observed_upsert(
        CriterionBank.upsert, writes, calls_at_first_write, lambda: counter["n"]
    )
    counter["n"] = 3
    observed(bank, "C3", True, "tool evidence", source="tool")
    observed(bank, "C3", True, "tool evidence again", source="tool")
    observed(bank, "C4", False, "fallback", source="fallback", evidence_sufficient=False)
    assert writes == ["C3"]
    assert calls_at_first_write == [3]
