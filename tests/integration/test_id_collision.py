"""Generated ticket IDs do not collide; aliases are deterministic.

Bounded in-process port of tests/integration/ticket-id-collision/run.sh (the bash
harness is being deleted). The bash probe generated N=100K ids; for pytest we use
a bounded N (default 2000, override via REBAR_ID_COLLISION_N) and assert the
load-bearing invariant: ZERO canonical-id collisions. Aliases are mnemonic helpers
(~1.5B combinations) and MAY collide by the birthday paradox, so we assert only
that ``compute_alias`` is deterministic and total (never raises, always a value)
for every generated id — not alias uniqueness.

Marked ``integration`` (opt-in): run with ``pytest -m integration``.
"""

from __future__ import annotations

import os

import pytest

from rebar._commands.composer import _new_ticket_id
from rebar.reducer._alias import compute_alias

pytestmark = pytest.mark.integration

_N = int(os.environ.get("REBAR_ID_COLLISION_N", "2000"))

_CANONICAL_RE = __import__("re").compile(r"^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$")


def test_zero_canonical_id_collisions() -> None:
    ids = [_new_ticket_id() for _ in range(_N)]
    for tid in ids:
        assert _CANONICAL_RE.match(tid), f"malformed canonical id: {tid!r}"
    assert len(set(ids)) == len(ids), (
        f"canonical id collision in {_N} generated ids ({len(ids) - len(set(ids))} duplicate(s))"
    )


def test_compute_alias_is_total_and_deterministic() -> None:
    ids = [_new_ticket_id() for _ in range(_N)]
    aliases = {}
    for tid in ids:
        alias = compute_alias(tid)
        assert alias, f"compute_alias returned empty for {tid!r}"
        # Deterministic: same id → same alias on a second call.
        assert compute_alias(tid) == alias, f"compute_alias non-deterministic for {tid!r}"
        aliases[tid] = alias
    # Sanity: aliases ARE mnemonic (adj-noun-noun), not just the hex id.
    assert any("-" in a for a in aliases.values())
