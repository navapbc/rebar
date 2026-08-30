"""``rebar export`` NDJSON must not put a JSON-uninteroperable integer on the wire,
and its ``rebar import`` twin must accept the decimal-string form losslessly
(bug ``guilty-pusslike-wyvern`` / ``a8db-dc3c-983a-40b0``).

The NDJSON export sibling of ``test_cli_js_safe_integers`` (bug e127). RFC 8259 §6
guarantees implementations "agree exactly on their numeric values" only for integers in
``[-(2**53)+1, (2**53)-1]``. rebar stamps ``time.time_ns()`` timestamps — 19 digits, far
outside that range — and ``rebar export`` emitted them as bare JSON numbers, so every
float64 consumer of the export artifact (``jq`` / ``node`` / a DuckDB or pandas JS-based
loader) read a SILENTLY WRONG value: a stored ``1787860170488898642`` comes back from
``node``'s ``JSON.parse`` as ``1787860170488898600``.

The fix routes the export byte-emit through the SAME proven choke point the MCP and CLI
``--output json`` surfaces use (``rebar._mcp_errors.js_safe_dumps`` / ``js_safe_result``),
so an out-of-range integer goes on the wire as its EXACT decimal string; and the import
reader coerces those provenance timestamps with ``int()`` so the store stays canonical and
the ``export | import`` round-trip preserves the EXACT nanosecond digits.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import rebar

pytestmark = pytest.mark.interface

#: RFC 8259 §6 interoperable integer range; also JS ``Number.MAX_SAFE_INTEGER``.
JS_SAFE_MAX = 2**53 - 1

#: A fixed nanosecond instant injected into the ``ns_ticket`` fixture through the HLC
#: clock seam (``REBAR_HLC_NOW``) so the fixture's stored timestamps are DETERMINISTIC
#: rather than a wall-clock draw. It is a multiple of 256: in ``[2**60, 2**61)`` the
#: float64 grid spacing (ULP) is 256, so an integer is float64-exact iff it is
#: ``≡ 0 mod 256``. Injecting this base makes the CREATE tick ``base + 1`` and the
#: comment tick ``base + 2`` — both float64-INEXACT — which is exactly the round-trip
#: hazard this suite must demonstrate. A raw wall-clock draw is float64-exact ~1/256 of
#: the time, which made ``test_export_timestamps_round_trip_exactly`` a data-dependent
#: flake (bug ``crashing-arachnidan-impala`` / ``d101-729a``).
_HLC_INEXACT_BASE_NS = 1788072768731609344  # % 256 == 0  →  base+1, base+2 are inexact


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


def _fresh_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _export_line(repo: Path, ticket_id: str) -> dict:
    """The single raw export line for ``ticket_id`` (parsed; int vs str preserved)."""
    buf = io.StringIO()
    rebar.export_tickets(out=buf, repo_root=str(repo))
    for raw in buf.getvalue().splitlines():
        obj = json.loads(raw)
        if obj.get("ticket_id") == ticket_id:
            return obj
    raise AssertionError(f"ticket {ticket_id} absent from export")


@pytest.fixture
def ns_ticket(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A real ticket whose stored ns timestamps are all beyond the JS-safe range AND
    deterministically float64-INEXACT.

    The stored instants are pinned via the ``REBAR_HLC_NOW`` clock seam (see
    ``_HLC_INEXACT_BASE_NS``) instead of a wall-clock draw. A wall-clock draw is
    float64-exact ~1/256 of the time near 1.79e18 (ULP == 256), which left the
    round-trip-hazard premise in ``test_export_timestamps_round_trip_exactly``
    undemonstrable on those draws — a data-dependent flake
    (bug ``crashing-arachnidan-impala`` / ``d101-729a``). The precondition below asserts
    the determinism so a regression to a raw wall-clock draw fails HERE, deterministically.
    """
    monkeypatch.setenv("REBAR_HLC_NOW", str(_HLC_INEXACT_BASE_NS))
    ticket_id = str(rebar.create_ticket("task", "a ticket carrying a nanosecond timestamp"))
    rebar.comment(ticket_id, "a comment, so comments[].timestamp is exercised too")
    stored = rebar.show_ticket(ticket_id)
    created_at = stored["created_at"]
    assert isinstance(created_at, int), f"created_at is not an int: {created_at!r}"
    assert created_at > JS_SAFE_MAX, (
        f"fixture precondition failed: created_at {created_at} is inside the JS-safe range, "
        "so this test could not detect the defect"
    )
    for field in ("created_at", "updated_at"):
        value = stored[field]
        assert int(float(value)) != value, (
            f"fixture precondition failed: stored {field} {value} is float64-EXACT, so the "
            "round-trip hazard is undemonstrable — the deterministic clock injection regressed "
            "(bug crashing-arachnidan-impala)"
        )
    return ticket_id


def test_export_emits_no_uninteroperable_integer(ns_ticket: str, rebar_repo: Path) -> None:
    """The reported mechanism: an export line must carry no bare >2**53 JSON number.

    LIVENESS FIRST — ``assert not hits`` alone passes vacuously on a payload with no
    timestamps. The anchors prove the fields under test were present and out-of-range
    before the absence assertion is allowed to mean anything.
    """
    line = _export_line(rebar_repo, ns_ticket)

    # Anchor: the fields this test polices are present and genuinely out of range.
    for field in ("created_at", "updated_at"):
        assert field in line, f"{field} missing; nothing to police: {line!r}"
    assert line.get("comments"), "no comments in the line; comments[].timestamp unexercised"
    assert int(line["created_at"]) > JS_SAFE_MAX, (
        f"created_at {line['created_at']!r} is inside the JS-safe range, so a broken guard "
        "would still pass this test"
    )
    assert int(line["comments"][0]["timestamp"]) > JS_SAFE_MAX, "comment ts inside JS-safe range"

    hits = _unsafe_ints(line)
    assert not hits, f"rebar export emitted uninteroperable integers: {hits}"


def test_export_timestamps_round_trip_exactly(ns_ticket: str, rebar_repo: Path) -> None:
    """The string form must be LOSSLESS — a rounded value would be the other bug.

    A JSON-*type*-only check would pass against a broken implementation that emitted
    ``str(float(value))``. This pins the exact digits against the float64 value a naive
    consumer would have got, so the assertion cannot be satisfied by a round-tripped double.
    """
    stored = rebar.show_ticket(ns_ticket)
    line = _export_line(rebar_repo, ns_ticket)

    for field in ("created_at", "updated_at"):
        on_wire = line[field]
        expected = stored[field]
        assert isinstance(on_wire, str), (
            f"{field} is still a bare JSON number ({on_wire!r}); a float64 consumer rounds it"
        )
        assert int(on_wire) == expected, f"{field} wire {on_wire!r} != stored {expected}"
        assert int(float(expected)) != expected, (
            f"stored {field} {expected} survives a float64 round-trip; the hazard is undemonstrable"
        )
        assert int(on_wire) != int(float(expected)), (
            f"{field} wire matches the float64-rounded {int(float(expected))}: precision lost"
        )

    comment_wire = line["comments"][0]["timestamp"]
    assert isinstance(comment_wire, str), f"comment timestamp still a bare number: {comment_wire!r}"
    assert int(comment_wire) == stored["comments"][0]["timestamp"]


def test_ns_ticket_timestamps_are_deterministically_float64_inexact(ns_ticket: str) -> None:
    """Regression guard for bug ``crashing-arachnidan-impala`` (``d101-729a``).

    The round-trip-hazard premise in ``test_export_timestamps_round_trip_exactly`` requires
    the stored ns instants to be float64-INEXACT. A raw wall-clock draw is float64-exact
    ~1/256 of the time (ULP == 256 near 1.79e18), which made that test a data-dependent
    flake. The ``ns_ticket`` fixture now injects a deterministic clock; this pins that
    guarantee so a regression to a wall-clock draw fails HERE, deterministically, rather
    than in ~1/256 of CI runs. It does not weaken any lossless-string assertion — it only
    proves the *premise's* data is deterministic.
    """
    stored = rebar.show_ticket(ns_ticket)
    for field in ("created_at", "updated_at"):
        value = stored[field]
        assert isinstance(value, int) and value > JS_SAFE_MAX, (
            f"{field} {value!r} is not an out-of-JS-range int; the hazard is unexercised"
        )
        assert int(float(value)) != value, (
            f"stored {field} {value} is float64-EXACT; the round-trip hazard is not "
            "deterministically demonstrable — the fixture's clock injection regressed"
        )


def test_export_import_round_trip_preserves_exact_ns(tmp_path: Path) -> None:
    """The acceptance-criterion round-trip: ``export | import`` preserves EXACT ns digits.

    Drives the whole pipeline: a real export (string wire form) imported into a fresh
    store must recover the source instant EXACTLY, as a canonical ``int`` provenance value.
    A float64 round-trip would give ``…600`` for ``…642``, so pinning the exact digits — not
    merely the type — is what makes this fail against a broken implementation.
    """
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")

    tid = str(rebar.create_ticket("task", "round-trip subject", repo_root=str(src)))
    rebar.comment(tid, "a provenance-bearing comment", repo_root=str(src))
    src_state = rebar.show_ticket(tid, repo_root=str(src))
    src_created = src_state["created_at"]
    src_comment_ts = src_state["comments"][0]["timestamp"]
    assert src_created > JS_SAFE_MAX and src_comment_ts > JS_SAFE_MAX

    buf = io.StringIO()
    rebar.export_tickets(out=buf, repo_root=str(src))
    ndjson = buf.getvalue()

    # The intermediate wire form must be the decimal string (else float64 consumers round it).
    wire_line = json.loads(ndjson.splitlines()[0])
    assert isinstance(wire_line["created_at"], str), (
        f"export put a bare number on the wire ({wire_line['created_at']!r}); the round-trip "
        "would silently survive here but external float64 consumers would not"
    )

    meta = rebar.import_tickets(ndjson.splitlines(), repo_root=str(dst))
    assert meta["created"] == 1 and not meta["warnings"], meta

    imported = rebar.list_tickets(repo_root=str(dst))[0]
    imported = rebar.show_ticket(imported["ticket_id"], repo_root=str(dst))

    src_prov = imported["source_created_at"]
    assert isinstance(src_prov, int), (
        f"imported source_created_at is {type(src_prov).__name__} {src_prov!r}; the reader must "
        "coerce the decimal-string wire form to a canonical int"
    )
    assert src_prov == src_created, (
        f"round-trip lost precision: source_created_at {src_prov} != original {src_created}"
    )

    comment_prov = imported["comments"][0]["source_created_at"]
    assert isinstance(comment_prov, int), (
        f"imported comment source_created_at is {type(comment_prov).__name__} {comment_prov!r}"
    )
    assert comment_prov == src_comment_ts, (
        f"comment round-trip lost precision: {comment_prov} != {src_comment_ts}"
    )


def test_import_accepts_decimal_string_timestamp(tmp_path: Path) -> None:
    """The import reader, driven directly, must coerce a decimal-STRING ns timestamp to int.

    Isolates the reader from export: a hand-built record with string timestamps (the wire
    form external and re-imported artifacts now carry) must land as a canonical ``int``
    provenance value with the EXACT digits — proving the round-trip cannot drift the type.
    """
    dst = _fresh_repo(tmp_path, "dst")
    created = 1787860170488898642  # a real 19-digit ns instant, > 2**53-1
    comment_ts = 1787860170499999999
    assert created > JS_SAFE_MAX and comment_ts > JS_SAFE_MAX

    record = {
        "ticket_id": "aaaa-bbbb-cccc-dddd",
        "ticket_type": "task",
        "title": "imported with string timestamps",
        "created_at": str(created),  # decimal-string wire form
        "author": "someone",
        "env_id": "src-env",
        "comments": [{"body": "c", "author": "someone", "timestamp": str(comment_ts)}],
        "schema_version": 2,
    }
    meta = rebar.import_tickets([record], repo_root=str(dst))
    assert meta["created"] == 1 and not meta["warnings"], meta

    imported = rebar.show_ticket(
        rebar.list_tickets(repo_root=str(dst))[0]["ticket_id"], repo_root=str(dst)
    )
    assert imported["source_created_at"] == created
    assert isinstance(imported["source_created_at"], int), "reader must coerce string ns -> int"
    assert imported["comments"][0]["source_created_at"] == comment_ts
    assert isinstance(imported["comments"][0]["source_created_at"], int)
