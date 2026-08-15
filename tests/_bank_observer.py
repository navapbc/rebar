"""Signature-faithful bookkeeping wrapper for ``CriterionBank.upsert``.

The completion-banking behavioral eval (``tests/external``) monkeypatches
``CriterionBank.upsert`` with a stub that records which criteria are banked while still
delegating to the real upsert. That stub MUST stay faithful to the real ``upsert``
signature: the real method carries keyword-only ``evidence_sufficient`` and ``seeded``
markers (framework-set on the bounded-fallback and cache-seed paths), and production passes
them. A stub that only accepts ``source`` silently drops those kwargs and raises
``TypeError`` the moment the verifier enters the bounded-fallback path — the exact drift that
broke the live bedrock arm (bug ``9c7c-4844-f53c-4eac``).

Factoring the wrapper here lets the live eval and a fast, live-dependency-free unit
regression (``tests/unit/test_completion_bank_observer_forwarding.py``) share one wrapper, so
any future kwarg added to ``upsert`` is exercised by the unit tier rather than only surfacing
on a live provider arm.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def make_observed_upsert(
    original_upsert: Callable[..., Any],
    writes: list[str],
    calls_at_first_write: list[int],
    evidence_calls: Callable[[], int],
) -> Callable[..., Any]:
    """Build a drop-in ``CriterionBank.upsert`` replacement that records the first ``tool``
    write of each criterion (into ``writes`` / ``calls_at_first_write``) and then forwards
    verbatim to ``original_upsert``.

    Every keyword argument other than ``source`` (which the bookkeeping inspects) is passed
    through unchanged via ``**kwargs``, so ``evidence_sufficient``, ``seeded``, and any future
    keyword-only param the real ``upsert`` grows flow through without the stub having to know
    about them.
    """

    def observed_upsert(
        self: Any,
        criterion_id: str,
        met: bool,
        evidence: str,
        *,
        source: str = "tool",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if source == "tool" and criterion_id not in writes:
            writes.append(criterion_id)
            calls_at_first_write.append(evidence_calls())
        return original_upsert(self, criterion_id, met, evidence, source=source, **kwargs)

    return observed_upsert
