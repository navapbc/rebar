"""Baseline document schema for the mechanism-delta ratchet (ticket 9ca8-675e-4dfb-427d).

The document is one UTF-8 JSON object with exactly ``schema_version`` (int ``1``) and
``mechanisms`` (object). Each ``mechanisms`` key is ``"<kind>::<name>"`` where ``<kind>``
is one of :data:`KINDS` and ``<name>`` is the mechanism's identity in the repository
(a lock class/file name, a ``REBAR_*`` variable, a ``<section>.<key>`` config path, a
gate-script path, a ``<file>::<fixture>`` site, a test-helper path). Each value is the
JSON integer ``1`` — a mechanism either exists or does not, so the value carries no
information beyond presence and is fixed so an "increased" bucket can never be forged by
hand-editing a count.

The validation posture is ported from ``scripts/check_complexity_baseline.py``: unknown or
missing top-level fields, duplicate JSON member names, unsorted keys, an unknown kind, an
empty name, a non-``1`` value, invalid UTF-8 and malformed JSON are all rejected.
"""

from __future__ import annotations

import json

# The seven mechanism kinds. They PARTITION the surface: every definition site yields
# exactly one ``(kind, name)`` entry, which is why ``feature_flag`` claims the
# boolean-coerced config keys and ``config_key`` claims only the non-boolean remainder.
KINDS: tuple[str, ...] = (
    "lock",
    "env_var",
    "config_key",
    "feature_flag",
    "ci_gate",
    "autouse_fixture",
    "test_helper",
)

SCHEMA_VERSION = 1


class SchemaError(Exception):
    """The baseline JSON document violates the baseline schema contract."""


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict:
    seen: set[str] = set()
    for name, _ in pairs:
        if name in seen:
            raise SchemaError(f"duplicate JSON member name: {name!r}")
        seen.add(name)
    return dict(pairs)


def split_key(key: str) -> tuple[str, str]:
    """Split ``"<kind>::<name>"`` into its parts, validating both halves."""
    if key.count("::") < 1:
        raise SchemaError(f"malformed baseline key (need '<kind>::<name>'): {key!r}")
    kind, name = key.split("::", 1)
    if kind not in KINDS:
        raise SchemaError(f"unknown mechanism kind {kind!r} in key {key!r}")
    if not name.strip():
        raise SchemaError(f"baseline key has an empty mechanism name: {key!r}")
    return kind, name


def _validate_presence(key: str, value: object) -> int:
    if type(value) is not int:  # excludes bool: type(True) is bool, not int
        raise SchemaError(f"value for {key!r} must be the JSON integer 1, got {value!r}")
    if value != 1:
        raise SchemaError(f"value for {key!r} must be 1 (presence), got {value}")
    return value


def parse_baseline(raw: str | bytes) -> dict[str, int]:
    """Validate a baseline document and return ``{"<kind>::<name>": 1}``."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SchemaError(f"baseline is not valid UTF-8: {exc}") from exc
    try:
        doc = json.loads(raw, object_pairs_hook=_reject_duplicate_members)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"baseline is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise SchemaError("baseline document must be a JSON object")
    if set(doc) != {"schema_version", "mechanisms"}:
        raise SchemaError(
            "baseline must have exactly 'schema_version' and 'mechanisms' fields; "
            f"got {sorted(doc)}"
        )
    if type(doc["schema_version"]) is not int or doc["schema_version"] != SCHEMA_VERSION:
        raise SchemaError(f"schema_version must be the integer {SCHEMA_VERSION}")
    mechanisms = doc["mechanisms"]
    if not isinstance(mechanisms, dict):
        raise SchemaError("'mechanisms' must be a JSON object")
    keys = list(mechanisms)
    if keys != sorted(keys):
        raise SchemaError("'mechanisms' keys must be sorted")
    entries: dict[str, int] = {}
    for key, value in mechanisms.items():
        split_key(key)
        entries[key] = _validate_presence(key, value)
    return entries


def render_baseline(entries: dict[str, int]) -> str:
    """Render a canonical, sorted baseline document (with a trailing newline)."""
    doc = {
        "schema_version": SCHEMA_VERSION,
        "mechanisms": {key: 1 for key in sorted(entries)},
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
