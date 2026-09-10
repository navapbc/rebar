"""Canonical JSON and SHA-256 serialization for the ticket store.

Every event writer uses :func:`canonical_bytes` or :func:`canonical_str` so
equivalent events produce identical committed bytes. The event form is
``json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)``
encoded as UTF-8 with no trailing newline. ``jq`` is excluded because it can
round nanosecond timestamps above 2^53 and corrupt the ordering key.
``tests/unit/test_canonical.py`` pins the byte contract and structure.
``tests/interfaces/store/test_canonical_event_bytes.py`` checks every producer.

This stdlib-only, lock-free module remains safe to import from transaction, link,
and delete writers that already hold their own lock. Re-serialization is
replay-safe because reducers consume parsed keys rather than source bytes.

Signing, workflow hashing, reconciler manifests, and provenance ledgers share
this canonical JSON and content-hash seam. Keyword-only options make intentional
encoding differences explicit while preserving positional call bytes.

- ``ascii_only`` defaults to literal UTF-8. ``True`` emits ``\\uXXXX`` escapes
  for ``mutation.serialize_manifest`` and ``conflict_resolver._hash_value``.
- ``default`` supplies the ``json.dumps`` fallback for values outside native JSON,
  including ``default=str`` in the provenance ledger.
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
