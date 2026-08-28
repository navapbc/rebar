"""CLI ``--output json`` must not emit a JSON-uninteroperable integer
(bug unhelping-creviced-rhino / e127-a3ad-895a-4a2f -- the CLI sibling of the MCP fix
unreal-milky-sloth / 6fe7-956f-4901-45cf).

RFC 8259 section 6 guarantees implementations "agree exactly on their numeric values" only
for integers in ``[-(2**53)+1, (2**53)-1]``. rebar's ticket timestamps are ``time.time_ns()``
values -- 19 digits, far outside that range -- and the CLI ``--output json`` emitters render
them with a bare ``json.dumps`` (``src/rebar/_engine_support/reads_cli.py``, ``signing.py``,
etc.), so they reach stdout as bare JSON numbers. Every float64-based consumer of that JSON --
``jq``, Node's ``JSON.parse``, Ruby -- SILENTLY rounds them
(``1787860170488898642`` -> ``1787860170488898600``, a -42 ns drift), and a lossless BigInt
consumer (GitHub Copilot CLI) instead dies re-stringifying the value with
``TypeError: Do not know how to serialize a BigInt``.

The fix routes CLI store-data serialization through the single ``js_safe_dumps`` choke point,
which emits an out-of-range integer as its EXACT decimal STRING. These tests assert BOTH
required properties of the fixed wire form:

  (a) ``int(wire_value) == stored_value`` -- the retype is lossless, not a rounding; and
  (b) the wire value is a JSON STRING, not a bare number -- the direct guard against BOTH the
      float64 silent-rounding AND the BigInt-serialization failure modes.

They drive the real in-process CLI arms against a real store. Nothing is mocked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import rebar
from rebar import signing
from rebar._engine_support import reads_cli

pytestmark = pytest.mark.unit

#: RFC 8259 section 6 interoperable integer range; also JS ``Number.MAX_SAFE_INTEGER``.
JS_SAFE_MAX = 2**53 - 1


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


def _run(*argv: str, capsys: pytest.CaptureFixture[str]) -> str:
    """Run a read subcommand in-process and return its stdout."""
    capsys.readouterr()
    rc = reads_cli.main(list(argv))
    out = capsys.readouterr().out
    assert rc == 0, f"`rebar {' '.join(argv)}` exited {rc}; stdout={out!r}"
    return out


@pytest.fixture
def ticket_with_ns_timestamp(rebar_repo: Path) -> str:
    """A real ticket whose stored ``created_at`` is a nanosecond int beyond the JS range."""
    ticket_id = rebar.create_ticket("task", "a ticket carrying a nanosecond timestamp")
    created_at = rebar.show_ticket(ticket_id)["created_at"]
    assert isinstance(created_at, int), f"created_at is not an int: {created_at!r}"
    assert created_at > JS_SAFE_MAX, (
        f"fixture precondition failed: created_at {created_at} is inside the JS-safe range, "
        "so this test could not detect the defect"
    )
    return str(ticket_id)


def test_show_json_emits_no_uninteroperable_integer(
    ticket_with_ns_timestamp: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`rebar show --output json`: the reported surface must carry no bare >2**53 number."""
    out = _run("show", ticket_with_ns_timestamp, "--output", "json", capsys=capsys)
    doc = json.loads(out)

    # Liveness: the field under test is present and was genuinely out of range.
    assert "created_at" in doc, f"created_at absent; nothing to police: {doc!r}"
    assert int(doc["created_at"]) > JS_SAFE_MAX, (
        f"created_at {doc['created_at']!r} is inside the JS-safe range; test is blind"
    )

    hits = _unsafe_ints(doc)
    assert not hits, f"show --output json emitted uninteroperable bare integers: {hits}"


def test_show_json_created_at_is_a_string_not_a_number(
    ticket_with_ns_timestamp: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Criterion (b): the wire value is a JSON STRING, the direct BigInt-serialization guard.

    Asserted against the RAW JSON bytes (``json.loads`` type + a quoted-token check), because
    a JS ``BigInt`` (and the ``TypeError`` it caused in the GitHub Copilot CLI) can only arise
    from a BARE numeric token -- a quoted string never becomes one.
    """
    out = _run("show", ticket_with_ns_timestamp, "--output", "json", capsys=capsys)
    doc = json.loads(out)

    assert isinstance(doc["created_at"], str), (
        f"created_at reached the wire as {type(doc['created_at']).__name__} "
        f"({doc['created_at']!r}) -- a bare out-of-range number, which a float64 consumer "
        "rounds and a BigInt consumer cannot re-serialize"
    )
    assert doc["created_at"].lstrip("-").isdigit(), (
        f"created_at is not bare decimal digits: {doc['created_at']!r}"
    )
    # The raw token must be quoted, i.e. a JSON string literal, never a bare number.
    stored = rebar.show_ticket(ticket_with_ns_timestamp)["created_at"]
    assert f'"created_at": "{stored}"' in out, (
        f"created_at is not a quoted JSON string in the raw output: {out!r}"
    )


def test_show_timestamp_round_trips_exactly(
    ticket_with_ns_timestamp: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Criterion (a): ``int(wire) == stored`` -- the retype is lossless, not a rounding.

    This is the mutation-teeth oracle: a lossy ``str(int(float(value)))`` "fix" would still
    produce a JSON string (passing the type check above) but a WRONG one, and only this exact
    round-trip assertion catches it.
    """
    stored = rebar.show_ticket(ticket_with_ns_timestamp)["created_at"]
    out = _run("show", ticket_with_ns_timestamp, "--output", "json", capsys=capsys)
    on_the_wire = json.loads(out)["created_at"]
    assert int(on_the_wire) == stored, (
        f"the wire value {on_the_wire!r} does not recover the stored {stored} -- the string "
        "form must be lossless, not rounded or scaled"
    )


def test_show_llm_emits_no_uninteroperable_integer(
    ticket_with_ns_timestamp: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`rebar show --output llm` rides the same emitter and must be JS-safe too.

    The llm projection omits ``created_at`` (verbose) but KEEPS ``updated_at``, so that field
    is the ns-timestamp this surface must not leak as a bare number.
    """
    out = _run("show", ticket_with_ns_timestamp, "--output", "llm", capsys=capsys)
    doc = json.loads(out)
    assert "updated_at" in doc, f"updated_at absent from llm projection; test is blind: {doc!r}"
    assert int(doc["updated_at"]) > JS_SAFE_MAX, "llm updated_at is inside the JS-safe range"
    assert isinstance(doc["updated_at"], str), (
        f"llm updated_at reached the wire as {type(doc['updated_at']).__name__}"
    )
    assert not _unsafe_ints(doc), f"show --output llm emitted bare big ints: {doc!r}"


def test_list_json_emits_no_uninteroperable_integer(
    ticket_with_ns_timestamp: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`rebar list --output json` returns many ticket records; none may carry a bare big int."""
    out = _run("list", "--status", "open", "--output", "json", capsys=capsys)
    rows = json.loads(out)

    # Liveness: real rows, carrying the field, genuinely out of range.
    assert isinstance(rows, list) and rows, f"list returned no rows: {rows!r}"
    ours = [r for r in rows if r.get("ticket_id") == ticket_with_ns_timestamp]
    assert ours, f"the ticket under test is absent from the listing: {ticket_with_ns_timestamp}"
    assert int(ours[0]["created_at"]) > JS_SAFE_MAX, "created_at is inside the JS-safe range"

    assert isinstance(ours[0]["created_at"], str), (
        f"list created_at reached the wire as {type(ours[0]['created_at']).__name__}"
    )
    assert not _unsafe_ints(rows), f"list --output json emitted bare big ints: {_unsafe_ints(rows)}"


def test_sign_json_signed_at_is_a_lossless_string(
    ticket_with_ns_timestamp: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`rebar sign --output json` emits ``signed_at`` (a ns int) -- it must be a lossless string."""
    capsys.readouterr()
    rc = signing.sign_cli(
        [ticket_with_ns_timestamp, json.dumps(["step one", "step two"]), "--output", "json"]
    )
    out = capsys.readouterr().out
    assert rc == 0, f"sign exited {rc}; stdout={out!r}"
    record = json.loads(out)

    assert "signed_at" in record, f"signed_at absent; nothing to police: {record!r}"
    assert int(record["signed_at"]) > JS_SAFE_MAX, "signed_at is inside the JS-safe range"
    assert isinstance(record["signed_at"], str), (
        f"signed_at reached the wire as {type(record['signed_at']).__name__} "
        f"({record['signed_at']!r})"
    )
    assert not _unsafe_ints(record), f"sign --output json emitted bare big ints: {record!r}"


# ─────────────────────── the Copilot-CLI-shaped consumer, end to end ───────────────────────

#: Reproduces the exact GitHub Copilot CLI failure mode from `_mcp_errors.py`: parse the CLI
#: JSON with a lossless BigInt reviver, then re-serialize. A BARE out-of-range number becomes a
#: JS ``BigInt`` that ``JSON.stringify`` cannot serialize (``TypeError: Do not know how to
#: serialize a BigInt``); a quoted STRING round-trips cleanly. The harness prints the recovered
#: decimal so the Python side can assert losslessness too.
_NODE_BIGINT_HARNESS = r"""
const fs = require("fs");
const raw = fs.readFileSync(0, "utf8");
const doc = JSON.parse(raw, (k, v) =>
  (k === "created_at" || k === "updated_at") && typeof v === "string" ? BigInt(v) : v);
// The line that threw for the real Copilot CLI on a bare numeric token:
const echoed = JSON.stringify({ created_at: doc.created_at.toString() });
process.stdout.write(echoed);
"""


def test_show_json_parses_in_a_bigint_consumer_without_error(
    ticket_with_ns_timestamp: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end guard against the documented BigInt-serialization failure mode.

    Self-skips when ``node`` is not on PATH (mirrors the repo's Node-backed e2e tier), so the
    Python assertions above remain the always-on floor.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("`node` not on PATH (install Node to run the BigInt-consumer assertion)")

    out = _run("show", ticket_with_ns_timestamp, "--output", "json", capsys=capsys)
    stored = rebar.show_ticket(ticket_with_ns_timestamp)["created_at"]

    proc = subprocess.run(
        [node, "-e", _NODE_BIGINT_HARNESS],
        input=out,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, (
        "a BigInt consumer (the GitHub Copilot CLI shape) failed on `show --output json`; this "
        f"is the `TypeError: Do not know how to serialize a BigInt` regression.\n"
        f"stderr={proc.stderr.strip()!r}\nstdout={proc.stdout.strip()!r}"
    )
    echoed = json.loads(proc.stdout)
    assert echoed["created_at"] == str(stored), (
        f"the BigInt consumer recovered {echoed['created_at']!r}, not the stored {stored} -- the "
        "string wire form must survive a lossless parse/serialize round-trip"
    )
