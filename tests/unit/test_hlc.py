"""Unit tests for the Hybrid Logical Clock (P2.1, epic snappy-weed-ruin).

Pins the four properties the design rests on: the ``REBAR_HLC`` kill-switch
(disabled → raw ``physical_now()``, today's behavior), strict monotonicity,
the per-ticket ``max(prefix)`` witness (the cross-clone causal floor, correct even
with NO cache file), and the injectable ``REBAR_HLC_NOW`` source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar._store import hlc


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    """A bare tracker dir with one ticket subdir (no events yet)."""
    trk = tmp_path / "repo" / ".tickets-tracker"
    (trk / "tk-1").mkdir(parents=True)
    return trk


def _write_event(tracker: Path, ticket_id: str, prefix: int, etype: str = "EDIT") -> None:
    (tracker / ticket_id / f"{prefix}-uuid-{etype}.json").write_text("{}", encoding="utf-8")


# ── kill-switch & injection ─────────────────────────────────────────────────
def test_physical_now_honors_injection(monkeypatch):
    monkeypatch.setenv("REBAR_HLC_NOW", "123456789012345678")
    assert hlc.physical_now() == 123456789012345678


def test_disabled_returns_physical_now_exactly(tracker, monkeypatch):
    monkeypatch.setenv("REBAR_HLC", "0")  # kill-switch
    monkeypatch.setenv("REBAR_HLC_NOW", "1700000000000000000")
    # Disabled path is exactly physical_now() — no +1, no witness, no cache.
    assert hlc.next_tick(tracker, "tk-1") == 1700000000000000000
    assert not (tracker.parent / ".rebar" / "hlc.state").exists()


def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv("REBAR_HLC", raising=False)
    assert hlc._enabled() is True
    monkeypatch.setenv("REBAR_HLC", "off")
    assert hlc._enabled() is False


# ── monotonicity & witness (enabled) ────────────────────────────────────────
def test_strictly_monotonic(tracker, monkeypatch):
    monkeypatch.delenv("REBAR_HLC", raising=False)
    monkeypatch.setenv("REBAR_HLC_NOW", "1700000000000000000")  # frozen physical clock
    ticks = [hlc.next_tick(tracker, "tk-1") for _ in range(50)]
    assert ticks == sorted(ticks)
    assert len(set(ticks)) == 50  # all unique
    assert ticks[0] > 1700000000000000000  # advanced past the frozen physical clock


def test_witness_max_prefix_floors_the_tick(tracker, monkeypatch):
    monkeypatch.delenv("REBAR_HLC", raising=False)
    # A pulled event whose prefix is far ABOVE the physical clock (a fast peer).
    big = 5_000_000_000_000_000_000
    monkeypatch.setenv("REBAR_HLC_NOW", "1700000000000000000")  # slow local clock
    _write_event(tracker, "tk-1", big)
    tick = hlc.next_tick(tracker, "tk-1")
    assert tick > big, "tick must exceed the witnessed max-prefix (causal floor)"


def test_witness_correct_with_no_cache_file(tracker, monkeypatch):
    # EXP4b: even with NO .rebar/hlc.state, the tick still exceeds the ticket's
    # max(prefix) — correctness is re-derived from the durable log.
    monkeypatch.delenv("REBAR_HLC", raising=False)
    monkeypatch.setenv("REBAR_HLC_NOW", "1700000000000000000")
    big = 5_000_000_000_000_000_000
    _write_event(tracker, "tk-1", big)
    assert not (tracker.parent / ".rebar" / "hlc.state").exists()
    assert hlc.next_tick(tracker, "tk-1") > big


def test_witness_is_per_ticket(tracker, monkeypatch):
    # A huge prefix on ANOTHER ticket must not floor this ticket's tick (the
    # witness is per-ticket; only the global cache carries cross-ticket high-water).
    monkeypatch.delenv("REBAR_HLC", raising=False)
    monkeypatch.setenv("REBAR_HLC_NOW", "1700000000000000000")
    (tracker / "tk-other").mkdir()
    _write_event(tracker, "tk-other", 9_000_000_000_000_000_000)
    tick = hlc.next_tick(tracker, "tk-1")
    assert tick < 9_000_000_000_000_000_000  # tk-other's prefix did not floor tk-1


def test_never_raises_on_missing_ticket_dir(tracker, monkeypatch):
    monkeypatch.delenv("REBAR_HLC", raising=False)
    # A ticket dir that does not exist yet (e.g. CREATE) must still return a tick.
    assert isinstance(hlc.next_tick(tracker, "does-not-exist-yet"), int)
