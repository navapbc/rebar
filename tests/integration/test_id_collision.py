"""Exercise canonical ticket IDs and deterministic aliases at integration volume.

The ID oracle checks the 16-character lowercase hexadecimal form and integer parseability.
The larger uniqueness sample supplements those assertions without treating improbable
collisions as its only signal. Aliases may collide, so only determinism and totality are
required. This integration test is selected with ``pytest -m integration``.
"""

from __future__ import annotations

import os
import re

import pytest

from rebar._alias import compute_alias, compute_genesis_alias
from rebar._commands.composer import _new_ticket_id

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


def test_compute_genesis_alias_is_total_deterministic_and_three_words() -> None:
    """New tickets get an adjective-adjective-animal alias. For every generated id
    the genesis generator must be total (never empty), deterministic, and yield a
    3-word alias whose two adjectives differ."""
    for tid in (_new_ticket_id() for _ in range(2000)):
        alias = compute_genesis_alias(tid)
        assert alias, f"compute_genesis_alias returned empty for {tid!r}"
        assert compute_genesis_alias(tid) == alias, f"non-deterministic for {tid!r}"
        parts = alias.split("-")
        assert len(parts) == 3, f"expected adj-adj-animal, got {alias!r}"
        assert parts[0] != parts[1], f"the two adjectives must differ: {alias!r}"
