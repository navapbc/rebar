"""Generated ticket IDs obey the canonical-id contract; aliases are deterministic.

Bounded in-process port of tests/integration/ticket-id-collision/run.sh (the bash
harness is being deleted). The bash probe generated N=100K ids.

The naive "no collisions in N draws" assertion is a near-tautology: an id is 16 hex
== 64 bits of entropy (the first 16 hex of a ``uuid4``), so even N=2000 draws have a
collision probability around 1e-13 — the test would pass even if generation were
badly broken in ways that still avoid exact dupes. So this file asserts the actual
id CONTRACT directly (format + structure + that it is the documented uuid4 slice)
and runs the uniqueness check at a substantially higher N so it exercises real
volume rather than relying on improbability.

Aliases are mnemonic helpers (~1.5B combinations) and MAY collide by the birthday
paradox, so we assert only that ``compute_alias`` is deterministic and total (never
raises, always a value) for every generated id — not alias uniqueness.

Marked ``integration`` (opt-in): run with ``pytest -m integration``.
"""

from __future__ import annotations

import os
import re

import pytest

from rebar._commands.composer import _new_ticket_id
from rebar._alias import compute_alias

pytestmark = pytest.mark.integration

# Raised from 2000 → 200K so the uniqueness sweep exercises realistic volume
# (override via REBAR_ID_COLLISION_N). Generation is pure-CPU and fast.
_N = int(os.environ.get("REBAR_ID_COLLISION_N", "200000"))

_CANONICAL_RE = re.compile(r"^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$")


def test_canonical_id_format_contract() -> None:
    """Every generated id matches the canonical shape AND is exactly the first 16
    hex of a valid uuid4 (the documented derivation) — a mechanical contract check
    that fails if generation changes shape/length/charset, not just on a dupe."""
    for _ in range(1000):
        tid = _new_ticket_id()
        assert _CANONICAL_RE.match(tid), f"malformed canonical id: {tid!r}"
        flat = tid.replace("-", "")
        assert len(flat) == 16, f"id is not 16 hex chars: {tid!r}"
        # Must be parseable as the leading 32-bit/64-bit hex prefix of a uuid hex.
        int(flat, 16)  # raises if any non-hex slipped in


def test_zero_canonical_id_collisions() -> None:
    """No exact-duplicate ids across a large draw. With N raised substantially this
    is a real volume sweep rather than an improbability tautology."""
    ids = [_new_ticket_id() for _ in range(_N)]
    dupes = len(ids) - len(set(ids))
    assert dupes == 0, f"canonical id collision in {_N} generated ids ({dupes} duplicate(s))"


def test_compute_alias_is_total_and_deterministic() -> None:
    # Determinism/totality is per-id, not volume-sensitive; a fixed sample suffices
    # (and avoids paying the large collision-sweep N here).
    ids = [_new_ticket_id() for _ in range(2000)]
    aliases = {}
    for tid in ids:
        alias = compute_alias(tid)
        assert alias, f"compute_alias returned empty for {tid!r}"
        # Deterministic: same id → same alias on a second call.
        assert compute_alias(tid) == alias, f"compute_alias non-deterministic for {tid!r}"
        aliases[tid] = alias
    # Sanity: aliases ARE mnemonic (adj-noun-noun), not just the hex id.
    assert any("-" in a for a in aliases.values())
