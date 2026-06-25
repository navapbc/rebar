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

This module is deliberately **lock-free and dependency-free** (stdlib ``json`` +
``hashlib`` only): the txn/link/delete paths rename+commit inline under their own
lock and must be able to import the serializer without pulling in the write lock.
Re-serialization is replay-safe — the reducer reads parsed keys, not raw bytes,
so routing an existing writer through this helper never changes replay behavior.

**Beyond the event path — the one true canonical-JSON/hash home.** This is also
the single seam for the other "sorted-key compact JSON (+ sha256)" sites that had
each reimplemented it inline (signing, workflow content-hash, reconciler manifest
+ provenance ledger). Two encoding axes are exposed as **additive, keyword-only**
parameters so a caller that deliberately diverges does so *explicitly* (a named
param with a cited consumer) rather than via a silent copy — and so the existing
positional callers ``canonical_str(event)`` / ``canonical_bytes(event)`` keep
their exact bytes untouched:

- ``ascii_only`` (``ensure_ascii``) — default ``False`` (literal UTF-8, the
  canonical event form). ``ascii_only=True`` reproduces a site that relied on the
  stdlib ``ensure_ascii=True`` default (``\\uXXXX`` escapes): the reconciler
  manifest (``mutation.serialize_manifest``) and provenance ledger
  (``conflict_resolver._hash_value``).
- ``default`` — the ``json.dumps`` fallback serializer for non-JSON-native values
  (e.g. ``default=str`` for the provenance ledger, which hashes arbitrary values).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_str(doc: Any, *, ascii_only: bool = False, default: Any = None) -> str:
    """The canonical committed text: sorted keys, compact separators,
    ``ensure_ascii=False`` (unless ``ascii_only``), no trailing newline.

    The keyword-only ``ascii_only`` / ``default`` params are additive: the
    positional ``canonical_str(doc)`` call is byte-identical to before.
    """
    return json.dumps(
        doc, ensure_ascii=ascii_only, separators=(",", ":"), sort_keys=True, default=default
    )


def canonical_bytes(doc: Any, *, ascii_only: bool = False, default: Any = None) -> bytes:
    """:func:`canonical_str` UTF-8 encoded — the exact bytes committed to the store."""
    return canonical_str(doc, ascii_only=ascii_only, default=default).encode("utf-8")


def content_hash(doc: Any, *, ascii_only: bool = False, default: Any = None) -> str:
    """Stable hex sha256 of :func:`canonical_bytes` — the one content-hash primitive."""
    return hashlib.sha256(canonical_bytes(doc, ascii_only=ascii_only, default=default)).hexdigest()
