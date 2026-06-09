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

from ticket_reducer._api import reduce_ticket, reduce_all_tickets
from ticket_reducer._state import make_error_dict, make_initial_state
from ticket_reducer._sort import event_sort_key
from ticket_reducer._inbound import find_inbound_relationships
from ticket_reducer._filters import apply_ticket_filters
from ticket_reducer.search import search_states
from ticket_reducer._cache import (
    compute_dir_hash,
    prepare_event_files,
    read_cache,
    write_cache,
)
from ticket_reducer._processors import (
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
from ticket_reducer.marker import check_marker, remove_marker, write_marker
from ticket_reducer.llm_format import to_llm

__all__ = [
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
