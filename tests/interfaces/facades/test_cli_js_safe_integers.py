"""CLI ``--output json`` must not put a JSON-uninteroperable integer on the wire
(bug unhelping-creviced-rhino / e127-a3ad-895a-4a2f).

The CLI sibling of ``test_mcp_js_safe_integers``. RFC 8259 section 6 guarantees that
implementations "agree exactly on their numeric values" only for integers in
``[-(2**53)+1, (2**53)-1]``. rebar stamps ``time.time_ns()`` timestamps -- 19 digits, far
outside that range -- and ``--output json`` emitted them as bare JSON numbers, so every
float64-based consumer of the surface where users are explicitly invited to pipe rebar
JSON into ``jq``/``node`` read a SILENTLY WRONG value: a stored
``1787860170488898642`` came back from ``node``'s ``JSON.parse`` as
``1787860170488898600``.

The fix routes the CLI JSON emitters through the SAME proven seam the MCP surface uses
(``rebar._mcp_errors.js_safe_result``), so an out-of-range integer goes on the wire as its
EXACT decimal string. These tests drive the real console script in a SUBPROCESS against a
real store, asserting on the bytes a shell pipeline would actually receive.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import rebar

pytestmark = pytest.mark.interface

#: RFC 8259 section 6 interoperable integer range; also JS ``Number.MAX_SAFE_INTEGER``.
JS_SAFE_MAX = 2**53 - 1

#: A fixed nanosecond instant injected into the ``ns_ticket`` fixture through the HLC
#: clock seam (``REBAR_HLC_NOW``) so the fixture's stored timestamps are DETERMINISTIC
#: rather than a wall-clock draw. It is a multiple of 256: in ``[2**60, 2**61)`` the
#: float64 grid spacing (ULP) is 256, so an integer is float64-exact iff it is
#: ``≡ 0 mod 256``. Injecting this base makes the CREATE tick ``base + 1`` and the
#: comment tick ``base + 2`` -- both float64-INEXACT -- so the round-trip-hazard premise in
#: ``test_show_timestamp_round_trips_exactly`` is always demonstrable. A raw wall-clock draw
#: is float64-exact ~1/256 of the time, which made that test a data-dependent flake
#: (bug ``crashing-arachnidan-impala`` / ``d101-729a``).
_HLC_INEXACT_BASE_NS = 1788072768731609344  # % 256 == 0  ->  base+1, base+2 are inexact


def _unsafe_ints(node: Any, path: str = "$") -> list[tuple[str, int]]:
    """Every integer in ``node`` that JSON cannot carry interoperably."""
    if isinstance(node, bool):
        return []
    if isinstance(node, int):
        return [(path, node)] if abs(node) > JS_SAFE_MAX else []
    if isinstance(node, dict):
        return [hit for k, v in node.items() for hit in _unsafe_ints(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [hit for i, v in enumerate(node) for hit in _unsafe_ints(v, f"{path}[{i}]")]
    return []


def _cli_json(*args: str) -> Any:
    """Run the real console script and parse its stdout as JSON (or NDJSON)."""
    proc = subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"rebar {' '.join(args)} failed ({proc.returncode}): {proc.stderr}"
    text = proc.stdout.strip()
    assert text, f"rebar {' '.join(args)} produced no stdout; stderr: {proc.stderr}"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture
def ns_ticket(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A real ticket whose stored ``created_at``/``updated_at`` are nanosecond ints beyond
    the JS range AND deterministically float64-INEXACT.

    The stored instants are pinned via the ``REBAR_HLC_NOW`` clock seam (see
    ``_HLC_INEXACT_BASE_NS``) instead of a wall-clock draw. A wall-clock draw is
    float64-exact ~1/256 of the time near 1.79e18 (ULP == 256), which left the
    round-trip-hazard premise in ``test_show_timestamp_round_trips_exactly`` undemonstrable
    on those draws -- a data-dependent flake (bug ``crashing-arachnidan-impala`` /
    ``d101-729a``). The precondition below asserts the determinism so a regression to a raw
    wall-clock draw fails HERE, deterministically.
    """
    monkeypatch.setenv("REBAR_HLC_NOW", str(_HLC_INEXACT_BASE_NS))
    ticket_id = str(rebar.create_ticket("task", "a ticket carrying a nanosecond timestamp"))
    rebar.comment(ticket_id, "a comment, so comments[].timestamp is exercised too")
    stored = rebar.show_ticket(ticket_id)
    created_at = stored["created_at"]
    # Prove the precondition: without an out-of-range value there is nothing to assert on.
    assert isinstance(created_at, int), f"created_at is not an int: {created_at!r}"
    assert created_at > JS_SAFE_MAX, (
        f"fixture precondition failed: created_at {created_at} is inside the JS-safe range, "
        "so this test could not detect the defect"
    )
    for field in ("created_at", "updated_at"):
        value = stored[field]
        assert int(float(value)) != value, (
            f"fixture precondition failed: stored {field} {value} is float64-EXACT, so the "
            "round-trip hazard is undemonstrable -- the deterministic clock injection regressed "
            "(bug crashing-arachnidan-impala)"
        )
    return ticket_id


def test_show_output_json_emits_no_uninteroperable_integer(ns_ticket: str) -> None:
    """The reported mechanism: ``rebar show --output json`` must carry no bare >2**53 number.

    LIVENESS FIRST. ``assert not hits`` alone passes vacuously -- on an error envelope, or
    on a payload carrying no timestamps at all. The anchors prove the payload actually
    contained the fields under test before the absence assertion is allowed to mean
    anything.
    """
    payload = _cli_json("show", ns_ticket, "--output", "json")

    # Anchor 1: this is the ticket under test, not an error envelope.
    assert payload.get("ticket_id") == ns_ticket, f"unexpected payload: {payload!r}"
    # Anchor 2: the fields this test polices are present.
    for field in ("created_at", "updated_at"):
        assert field in payload, f"{field} missing; nothing to police: {payload!r}"
    assert payload.get("comments"), "no comments in the payload; comments[].timestamp unexercised"
    # Anchor 3: the value is genuinely outside the safe range, so the guard had work to do.
    assert int(payload["created_at"]) > JS_SAFE_MAX, (
        f"created_at {payload['created_at']!r} is inside the JS-safe range, so a broken "
        "guard would still pass this test"
    )

    hits = _unsafe_ints(payload)
    assert not hits, f"rebar show --output json emitted uninteroperable integers: {hits}"


def test_show_timestamp_round_trips_exactly(ns_ticket: str) -> None:
    """The string form must be LOSSLESS -- a rounded value would be the other bug.

    Checking only the JSON *type* would pass against a broken implementation that emitted
    ``str(float(value))``. This pins the exact digits, and pins them against the float64
    value a naive consumer would have got, so the assertion cannot be satisfied by a
    round-tripped double.
    """
    stored = rebar.show_ticket(ns_ticket)["created_at"]
    payload = _cli_json("show", ns_ticket, "--output", "json")
    on_the_wire = payload["created_at"]

    assert isinstance(on_the_wire, str), (
        f"created_at is still a bare JSON number ({on_the_wire!r}); a float64 consumer "
        "rounds it silently"
    )
    assert int(on_the_wire) == stored, (
        f"the wire value {on_the_wire!r} does not recover the stored {stored}"
    )
    # The discriminating half: a float64 round-trip LOSES digits, so a passing
    # implementation cannot be one that went through a double on the way out.
    assert int(float(stored)) != stored, (
        f"stored {stored} survives a float64 round-trip, so this repo's timestamps cannot "
        "demonstrate the hazard and the assertion below proves nothing"
    )
    assert int(on_the_wire) != int(float(stored)), (
        f"the wire value matches the float64-rounded {int(float(stored))}, i.e. precision "
        "was lost on the way out"
    )


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("list", "--output", "json"), id="list"),
        pytest.param(("ready", "--output", "json"), id="ready"),
        pytest.param(("show", "--output", "llm"), id="show-llm"),
        pytest.param(("list", "--output", "llm"), id="list-llm"),
    ],
)
def test_multi_ticket_json_surfaces_are_js_safe(ns_ticket: str, argv: tuple[str, ...]) -> None:
    """The other ticket-state-bearing ``--output json``/``llm`` reads share the exposure."""
    args = (argv[0], ns_ticket, *argv[1:]) if argv[0] == "show" else argv
    payload = _cli_json(*args)

    rows = payload if isinstance(payload, list) else [payload]
    assert rows, f"rebar {' '.join(args)} returned no rows; nothing to police"
    # ``updated_at`` is the one ns timestamp every projection here carries (the ``llm``
    # view drops ``created_at``), so it is the anchor that proves the payload really
    # contained an out-of-range value for the guard to act on.
    flat = json.dumps(rows)
    stored_updated = rebar.show_ticket(ns_ticket)["updated_at"]
    assert stored_updated > JS_SAFE_MAX, "updated_at is inside the JS-safe range"
    assert str(stored_updated) in flat, (
        f"the ticket under test is absent from {' '.join(args)}; the assertion below is vacuous"
    )

    hits = _unsafe_ints(rows)
    assert not hits, f"rebar {' '.join(args)} emitted uninteroperable integers: {hits}"


def test_sign_and_verify_signature_json_are_js_safe(ns_ticket: str) -> None:
    """``signed_at`` is a ns timestamp on two more ``--output json`` surfaces."""
    signed = _cli_json("sign", ns_ticket, '["a-verified-step"]', "--output", "json")
    assert "signed_at" in signed, f"signed_at missing from sign output: {signed!r}"
    assert int(signed["signed_at"]) > JS_SAFE_MAX, "signed_at is inside the JS-safe range"
    assert not _unsafe_ints(signed), f"rebar sign emitted uninteroperable integers: {signed!r}"

    verified = _cli_json("verify-signature", ns_ticket, "--output", "json")
    assert int(verified["signed_at"]) == int(signed["signed_at"]), (
        "verify-signature lost or changed the signing instant"
    )
    assert not _unsafe_ints(verified), (
        f"rebar verify-signature emitted uninteroperable integers: {verified!r}"
    )


def test_audit_show_json_is_js_safe(ns_ticket: str) -> None:
    """``rebar audit show --output json`` nests the whole ticket state one level down."""
    payload = _cli_json("audit", "show", ns_ticket, "--output", "json")

    ticket = payload.get("ticket") or {}
    assert ticket.get("ticket_id") == ns_ticket, f"unexpected audit payload: {payload!r}"
    assert int(ticket["created_at"]) > JS_SAFE_MAX, "created_at is inside the JS-safe range"

    hits = _unsafe_ints(payload)
    assert not hits, f"rebar audit show emitted uninteroperable integers: {hits}"


def test_review_plan_status_json_is_js_safe(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``rebar review-plan <id> --status --output json`` carries its own ``signed_at``.

    Driven IN-PROCESS with a stubbed ``plan_review_status`` rather than through the
    subprocess helper: a real, non-null ``signed_at`` needs a certified plan-review
    attestation, and earning one is a billable multi-call LLM run. The stub is the
    payload SHAPE the emitter is handed (``plan_review_status.schema.json``), so what is
    under test here -- the emitter's wire form -- is exercised for real.
    """
    import rebar.llm
    from rebar._cli import _llm_commands

    ticket_id = str(rebar.create_ticket("task", "a ticket for the review-plan status emitter"))
    signed_at = 1787860170488898642  # a real 19-digit ns instant, > 2**53-1
    assert signed_at > JS_SAFE_MAX, "the stub value must be outside the JS-safe range"

    # ``_review_plan`` does a FUNCTION-LOCAL ``from rebar import llm``, so the attribute is
    # resolved off the module at call time -- patch it on ``rebar.llm`` itself.
    monkeypatch.setattr(
        rebar.llm,
        "plan_review_status",
        lambda *_a, **_k: {
            "ok": True,
            "verdict": "certified",
            "reason": "current",
            "verified_at_sha": "0" * 40,
            "signed_at": signed_at,
            "currency_basis": "code",
        },
        raising=False,
    )

    rc = _llm_commands._review_plan([ticket_id, "--status", "--output", "json"])
    assert rc == 0, "the status read should succeed"

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["signed_at"] == str(signed_at), (
        f"signed_at went out as {payload['signed_at']!r}; a float64 consumer rounds a bare "
        "19-digit number"
    )
    assert int(payload["signed_at"]) == signed_at, "the wire value lost digits"
    assert not _unsafe_ints(payload), (
        f"review-plan --status emitted uninteroperable integers: {payload!r}"
    )
