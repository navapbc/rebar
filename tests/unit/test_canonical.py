"""Unit tests for rebar._store.canonical — the single event-byte serializer (P1.0).

Two contracts are pinned here:

* The canonical byte format itself — sorted keys, compact separators,
  ``ensure_ascii=False``, no trailing newline — including the two fuzz cases the
  epic calls out: a >2^53 ns ``timestamp`` (must round-trip exactly, never via a
  float) and non-ASCII text (must be emitted literally, not ``\\uXXXX``).
* The **structural guard**: a stdlib AST/regex scan of ``src/rebar`` that fails
  if any writer serializes an event dict with a raw ``json.dump(s)`` instead of
  routing through this helper. After P1.0 the expected hit set is EMPTY — every
  live event writer goes through ``canonical_str``/``canonical_bytes``.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from rebar._store.canonical import canonical_bytes, canonical_str

_SRC = Path(__file__).resolve().parents[2] / "src" / "rebar"


def _event(**over):
    e = {
        "uuid": "u-1",
        "event_type": "COMMENT",
        "timestamp": 1700000000000000000,
        "env_id": "e",
        "author": "a",
        "data": {"body": "x"},
    }
    e.update(over)
    return e


# ── the canonical byte contract ────────────────────────────────────────────────
def test_canonical_str_is_sorted_compact_no_newline():
    # Keys deliberately out of order; nested dict too.
    raw = canonical_str(_event(data={"z": 1, "a": 2}))
    # sorted top-level keys (author < data < env_id < event_type < timestamp < uuid)
    assert raw.startswith('{"author":')
    assert '"data":{"a":2,"z":1}' in raw  # nested keys sorted too
    assert ", " not in raw and ": " not in raw  # compact separators
    assert not raw.endswith("\n")


def test_canonical_bytes_is_str_utf8_encoded():
    ev = _event(data={"body": "héllo 世界"})
    assert canonical_bytes(ev) == canonical_str(ev).encode("utf-8")


def test_non_ascii_is_emitted_literally_not_escaped():
    raw = canonical_bytes(_event(data={"body": "世界"}))
    assert "世界".encode() in raw  # real UTF-8 bytes …
    assert b"\\u" not in raw  # … not \uXXXX escapes


def test_large_timestamp_round_trips_exactly():
    # The ns timestamp is a 19-digit int > 2^53; it must survive serialize→parse
    # with no float rounding (the jq-parses-as-float64 footgun the epic flags).
    ts = 1781386734104724847
    assert ts > 2**53
    ev = _event(timestamp=ts)
    raw = canonical_bytes(ev)
    assert str(ts).encode() in raw  # written as the exact integer literal
    parsed = json.loads(raw)
    assert parsed["timestamp"] == ts and isinstance(parsed["timestamp"], int)


def test_reserialization_is_idempotent():
    ev = _event(data={"body": "世界", "n": 2**60, "z": [3, 2, 1]})
    once = canonical_bytes(ev)
    assert canonical_bytes(json.loads(once)) == once


# ── structural guard: no writer bypasses the helper ─────────────────────────────
# Mirrors docs/experiments/event_write_guard.py (the validated EXP-R9 prototype):
# flag any json.dump(s) whose serialized arg is an event dict (carries
# "event_type"). The canonical helper lives in canonical.py and takes a bare
# parameter, so it is not itself a hit.
_PY_ALLOW = {"_store/canonical.py"}
_SH_ALLOW_SUBSTR = ("ticket-create.sh", "ticket-edit.sh", "ticket-migrate")
_SH_EVENT = re.compile(r"""['"]event_type['"]""")
_SH_DUMP = re.compile(r"json\.dumps?\s*\(")


def _has_event_type(node) -> bool:
    return isinstance(node, ast.Dict) and any(
        isinstance(k, ast.Constant) and k.value == "event_type" for k in node.keys
    )


def _scan_python(root: Path) -> list[str]:
    hits: list[str] = []
    for p in root.rglob("*.py"):
        rel = p.as_posix()
        if any(rel.endswith(a) for a in _PY_ALLOW):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        evvars = {
            t.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and _has_event_type(n.value)
            for t in n.targets
            if isinstance(t, ast.Name)
        }
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("dump", "dumps")
            ):
                a0 = n.args[0] if n.args else None
                if _has_event_type(a0) or (isinstance(a0, ast.Name) and a0.id in evvars):
                    hits.append(f"{rel}:{n.lineno}")
    return hits


def _scan_bash(root: Path) -> list[str]:
    hits: list[str] = []
    for p in root.rglob("*.sh"):
        if any(s in p.name for s in _SH_ALLOW_SUBSTR):
            continue
        body = p.read_text(encoding="utf-8", errors="replace")
        if _SH_DUMP.search(body) and _SH_EVENT.search(body):
            hits.append(p.as_posix())
    return hits


def test_no_raw_event_serializers_in_src():
    py = _scan_python(_SRC)
    sh = _scan_bash(_SRC)
    assert py == [], f"raw Python event serializer(s) must use canonical_bytes: {py}"
    assert sh == [], f"bash event-write heredoc(s) must call the canonical helper: {sh}"
