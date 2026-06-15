"""Ticket reducer processor package.

Provides event-type processors, state helpers, sort utilities, cache
management, and the public reduce API.

Public re-exports:
    reduce_ticket, reduce_all_tickets     — from _api (primary entry points)
    make_initial_state, make_error_dict   — from _state
    event_sort_key                        — from _sort
    compute_dir_hash, read_cache,
    write_cache                           — from _cache
    process_create, process_status,
    process_comment, process_link,
    process_unlink, process_bridge_alert,
    process_revert, process_edit,
    process_archived, process_snapshot,
    scan_for_latest_snapshot              — from _processors
    to_llm                                — from llm_format
"""

from ._api import reduce_all_tickets, reduce_ticket
from ._cache import (
    compute_dir_hash,
    prepare_event_files,
    read_cache,
    write_cache,
)
from ._filters import apply_ticket_filters
from ._inbound import find_inbound_relationships
from ._processors import (
    process_archived,
    process_bridge_alert,
    process_comment,
    process_create,
    process_edit,
    process_link,
    process_revert,
    process_snapshot,
    process_status,
    process_unlink,
    replay_events,
    scan_for_latest_snapshot,
)
from ._sort import event_sort_key
from ._state import make_error_dict, make_initial_state
from ._version import KNOWN_EVENT_TYPES, SCHEMA_VERSION
from .llm_format import to_llm
from .marker import check_marker, remove_marker, write_marker
from .search import search_states

__all__ = [
    "SCHEMA_VERSION",
    "KNOWN_EVENT_TYPES",
    "reduce_ticket",
    "reduce_all_tickets",
    "find_inbound_relationships",
    "apply_ticket_filters",
    "search_states",
    "make_initial_state",
    "make_error_dict",
    "event_sort_key",
    "compute_dir_hash",
    "prepare_event_files",
    "read_cache",
    "write_cache",
    "process_create",
    "process_status",
    "process_comment",
    "process_link",
    "process_unlink",
    "process_bridge_alert",
    "process_revert",
    "process_edit",
    "process_archived",
    "process_snapshot",
    "scan_for_latest_snapshot",
    "replay_events",
    "write_marker",
    "remove_marker",
    "check_marker",
    "to_llm",
]
