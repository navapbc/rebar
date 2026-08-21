"""Held-out validation for bug 2d1c — authored independently of the implementation.

`rebar review-code --ref <sha> --source attested` accepted both values and dropped
them at the shim, so `--source attested` silently reviewed the dirty working tree and
returned a result indistinguishable from a successful attested run. `_mcp_llm` tells
MCP callers the result carries `source`/`verified_at_sha`/`signable`, so this was a
false promise on the boundary that decides whether a code review can be SIGNED.

That framing sets what is worth testing. The dangerous direction is not "provenance is
missing" (loud, caught immediately) but "provenance is present and WRONG" — above all a
result that claims to be signable when nothing was pinned. These tests hammer that:
every path must carry the three keys, and no path that failed to pin a source may ever
come back signable.

Assertions are on the returned contract and on what reaches the gate request; nothing
pins private names or source text.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar.llm import gate_source
from rebar.llm.code_review import shim

PROVENANCE = ("source", "verified_at_sha", "signable")


def _enable(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any], verdict: dict) -> None:
    """Enable the gate and stub the four-pass run, capturing the request it receives."""
    from rebar.llm.workflow import gate_dispatch

    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda _r: True)

    def _produce(request: Any) -> dict:
        captured["request"] = request
        return verdict

    monkeypatch.setattr(gate_dispatch, "produce_code_review_verdict", _produce)


def _verdict(**over: Any) -> dict:
    base = {"verdict": "PASS", "blocking": [], "advisory": [], "runner": "stub", "coverage": {}}
    base.update(over)
    return base


# ── the caller's request actually reaches the gate ──────────────────────────


def test_source_reaches_the_gate_request(monkeypatch: pytest.MonkeyPatch) -> None:
    cap: dict[str, Any] = {}
    _enable(monkeypatch, cap, _verdict())

    shim.review_code(source="attested", ref="cafebabe1234", diff_text="d")

    assert cap["request"].source == "attested"


def test_explicit_ref_selects_the_reviewed_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate pins exactly one ref (`request.head`). An explicit `--ref` must become
    that commit, or the snapshot is pinned somewhere other than what is being reviewed."""
    cap: dict[str, Any] = {}
    _enable(monkeypatch, cap, _verdict())

    shim.review_code(ref="cafebabe1234", base="HEAD~3", diff_text="d")

    assert cap["request"].head == "cafebabe1234"
    assert cap["request"].base == "HEAD~3", "an explicit ref must not disturb the range base"


def test_absent_ref_leaves_head_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative control for the mapping: no `ref` means `head` is untouched, so the
    override cannot be an unconditional clobber."""
    cap: dict[str, Any] = {}
    _enable(monkeypatch, cap, _verdict())

    shim.review_code(head="my-head", diff_text="d")

    assert cap["request"].head == "my-head"


# ── the returned contract is complete on EVERY path ─────────────────────────


def test_result_carries_all_three_provenance_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_mcp_llm` promises these keys unconditionally."""
    cap: dict[str, Any] = {}
    _enable(monkeypatch, cap, _verdict(source="local", verified_at_sha=None, signable=False))

    result = shim.review_code(diff_text="d")

    for key in PROVENANCE:
        assert key in result, f"the documented MCP promise requires {key!r}"


def test_config_off_explicit_call_reaches_the_gate_enabled_and_stays_unpinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The config key no longer gates the explicit surface (bug 5b32-37c4-f99a-4315): with
    `verify.enable_code_review` off, the shim still dispatches `enabled=True`. A verdict
    that pinned nothing must be honestly unpinned — NOT signable: `signable: True` here
    would let an unreviewed change be certified, the worst reachable outcome."""
    from rebar.llm.workflow import gate_dispatch

    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda _r: False)
    cap: dict[str, Any] = {}

    def _produce(request: Any) -> dict:
        cap["request"] = request
        return _verdict()

    monkeypatch.setattr(gate_dispatch, "produce_code_review_verdict", _produce)

    result = shim.review_code(source="attested", ref="cafebabe1234", diff_text="d")

    assert cap["request"].enabled is True  # explicit intent, never the config key
    for key in PROVENANCE:
        assert key in result
    assert result["signable"] is False, "an unpinned result must never be signable"
    assert result["verified_at_sha"] is None


# ── provenance is never FABRICATED ──────────────────────────────────────────


def test_a_verdict_that_pinned_nothing_yields_an_unsignable_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preflight-degraded verdict never resolved a handle. Claiming a source it did
    not have -- e.g. echoing back the REQUESTED `attested` -- would be exactly the
    silent-unsignable-attested-run confusion this ticket exists to remove."""
    cap: dict[str, Any] = {}
    _enable(monkeypatch, cap, _verdict(verdict="INDETERMINATE"))  # no provenance on it

    result = shim.review_code(source="attested", ref="cafebabe1234", diff_text="d")

    assert result["signable"] is False
    assert result["verified_at_sha"] is None
    assert result["source"] is None, (
        "a run that pinned nothing must not report the REQUESTED source as achieved"
    )


def test_the_gates_own_stamp_wins_over_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The result must describe what the review ACTUALLY ran under, not what was asked
    for. A caller asking for `attested` that resolves to `local` must see `local`."""
    cap: dict[str, Any] = {}
    _enable(
        monkeypatch,
        cap,
        _verdict(source="local", verified_at_sha=None, signable=False),
    )

    result = shim.review_code(source="attested", diff_text="d")

    assert result["source"] == "local"
    assert result["signable"] is False


def test_attested_stamp_is_propagated_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    cap: dict[str, Any] = {}
    sha = "a" * 40
    _enable(monkeypatch, cap, _verdict(source="attested", verified_at_sha=sha, signable=True))

    result = shim.review_code(source="attested", ref=sha, diff_text="d")

    assert result["source"] == "attested"
    assert result["verified_at_sha"] == sha
    assert result["signable"] is True


# ── the shared helper's own contract ────────────────────────────────────────


def test_copy_provenance_defaults_to_unpinned_for_a_missing_source() -> None:
    dst: dict[str, Any] = {}
    gate_source.copy_provenance(None, dst)
    assert dst["signable"] is False
    assert dst["verified_at_sha"] is None
    assert dst["source"] is None
