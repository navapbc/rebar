"""The single canonical event-byte serializer for the tickets store (P1.0).

Every writer that emits an event file MUST route its serialization through
:func:`canonical_bytes` (or :func:`canonical_str`) so the committed bytes are
**byte-identical regardless of which writer produced them** — the property the
HLC/signature/tag-convergence work (P2.x) rests on. Before this helper, the
canonical form was duplicated inline in ``event_append.py`` while seven other
live writers emitted plain unsorted ``json.dumps`` — diverging bytes for the same
logical event.

**The canonical form.** ``json.dumps(event, ensure_ascii=False,
separators=(",", ":"), sort_keys=True)`` with NO trailing newline — sorted keys,
compact separators, real UTF-8 (not ``\\uXXXX``). This is byte-equal to
``jq -S -c '.'`` *only* with ``ensure_ascii=False`` (non-ASCII like ``世界``
diverges otherwise). Parity is asserted Python↔Python, **never** Python↔jq: jq
parses the >2^53 ns ``timestamp`` as float64 and rounds it (jq ≤1.6 on parse,
jq-1.7 under arithmetic), which would both break parity and corrupt the ordering
key — so jq is kept out of the event path entirely (the TUF/sigstore lesson:
"canonicalization across implementations is a footgun; standardize on one").

Parity is pinned by ``tests/unit/test_canonical.py`` (the byte contract + the
structural guard) and ``tests/interfaces/store/test_canonical_event_bytes.py``
(every committed event file, written by any live producer, equals
``canonical_bytes`` of its own parsed content).

This module is deliberately **lock-free and dependency-free** (stdlib ``json``
only): the txn/link/delete paths rename+commit inline under their own lock and
must be able to import the serializer without pulling in the write lock.
Re-serialization is replay-safe — the reducer reads parsed keys, not raw bytes,
so routing an existing writer through this helper never changes replay behavior.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_str(event: dict[str, Any]) -> str:
    """The canonical committed text: sorted keys, compact separators,
    ``ensure_ascii=False``, no trailing newline."""
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_bytes(event: dict[str, Any]) -> bytes:
    """:func:`canonical_str` UTF-8 encoded — the exact bytes committed to the store."""
    return canonical_str(event).encode("utf-8")
