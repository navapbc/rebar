"""The Tier-0 candidate registry (ticket bouncy-peacockish-titmouse / 5d19-52e0-7c26-47fb).

A ``Candidate`` names one alternative Pass-3 configuration to replay the corpus against:
a per-criterion ``(block_threshold, blocking_enabled)`` overlay plus an ``impact_fn``. The
built-in ``"current"`` entry carries an empty overlay and the default ``impact_fn``, so
replaying it reproduces live production behavior exactly (the harness's own identity
check). Add a new candidate by adding one entry to :data:`CANDIDATES` — no dynamic
loader, no directory convention.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rebar.llm.review_kernel import decide


@dataclass(frozen=True)
class Candidate:
    #: Per-criterion ``(block_threshold, blocking_enabled)`` overrides. A criterion not
    #: present here falls back to the live registry's resolution.
    overlay: dict[str, tuple[float, bool]] = field(default_factory=dict)
    #: The impact function threaded to Pass-3. Defaults to the plan-review impact model.
    impact_fn: Callable[[dict[str, Any]], float] = decide.impact_plan


CANDIDATES: dict[str, Candidate] = {
    "current": Candidate(),
}
