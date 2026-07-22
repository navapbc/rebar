"""JiraIdentityConvention — the ``rebar-id:`` back-pointer label convention (S2).

The convention reproduces the previously-inlined behaviour EXACTLY (see
``binding_walk._has_rebar_id_label`` and the ``f"rebar-id:{local_id}"`` write sites):

* writes use the canonical colon form ``rebar-id:<local_id>``;
* reads accept a label as an identity marker iff it starts with ``"rebar-id:"`` OR
  ``"rebar-id-"`` AND the remainder after the prefix is non-empty after ``.strip()``.
"""

from __future__ import annotations

_CANONICAL_PREFIX = "rebar-id:"
_READ_PREFIXES = ("rebar-id:", "rebar-id-")


class JiraIdentityConvention:
    """Formats/parses the Jira ``rebar-id:<local_id>`` identity label."""

    def format_label(self, local_id: str) -> str:
        return f"{_CANONICAL_PREFIX}{local_id}"

    def parse_label(self, label: str) -> str | None:
        for prefix in _READ_PREFIXES:
            if label.startswith(prefix):
                remainder = label[len(prefix) :]
                if remainder.strip():
                    return remainder
        return None

    def is_identity_label(self, label: str) -> bool:
        return self.parse_label(label) is not None
