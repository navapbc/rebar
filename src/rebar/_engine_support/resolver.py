"""Shared Python ticket-ID resolver — re-export shim over the ``rebar._ids`` leaf.

The resolution primitives moved DOWN to the stdlib-only leaf ``rebar._ids`` so the
pure event-replay layer (``rebar.reducer``) can depend on them without reaching UP
into ``_engine_support`` (a layering inversion + import cycle). This module keeps
its historical public surface — ``resolve_ticket_id`` and the alias/binding
helpers — by re-exporting them, so every existing
``from rebar._engine_support.resolver import …`` importer is unchanged. New code
should import from ``rebar._ids`` directly. See ``rebar._ids`` for the full
resolution semantics (ID forms, alias scan, Jira-key binding-store lookup).
"""

from __future__ import annotations

from rebar._ids import (
    _FULL_ID_RE as _FULL_ID_RE,
)
from rebar._ids import (
    _JIRA_KEY_RE as _JIRA_KEY_RE,
)
from rebar._ids import (
    _SHORT_ID_RE as _SHORT_ID_RE,
)
from rebar._ids import (
    _resolve_via_binding_store as _resolve_via_binding_store,
)
from rebar._ids import (
    _scan_alias as _scan_alias,
)
from rebar._ids import (
    resolve_ticket_id as resolve_ticket_id,
)

__all__ = [
    "resolve_ticket_id",
    "_resolve_via_binding_store",
    "_scan_alias",
    "_FULL_ID_RE",
    "_SHORT_ID_RE",
    "_JIRA_KEY_RE",
]
