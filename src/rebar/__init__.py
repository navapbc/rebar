"""Public facade for rebar's git-backed ticket system and Jira reconciler.

The ``rebar`` and ``rebar-mcp`` console scripts expose the same implementation as
this in-process API. Ticket and bridge operations enter through this facade;
``rebar.reducer`` and ``rebar.graph`` remain available for bulk reads.

Implementations live in size-bounded topical modules without changing imports:
``_lib_writes`` owns lifecycle, mutations, signing, and ``_python_leaf``;
``_lib_gates`` owns quality gates and grounding; ``_lib_reads`` owns queries,
import/export, fsck, and ``_json_or``; and ``_lib_ops`` owns workflows and explicit
bridge operations.
"""

from __future__ import annotations

import importlib.metadata
import logging

from rebar import config

# Re-export config-read failures so verify.* gates fail closed and callers can catch
# ``rebar.ConfigError`` beside ``rebar.RebarError``.
from rebar._config_coercion import ConfigError
from rebar._engine import engine_dir

# Exceptions live in the stdlib-only ``_errors`` leaf; re-exporting them preserves
# the established ``rebar.RebarError`` and ``from rebar import …`` imports.
from rebar._errors import (
    KNOWN_ERROR_CODES,
    ConcurrencyError,
    RebarError,
    error_code_for,
)
from rebar._lib_gates import (
    check_ac,
    clarity_check,
    declare_no_file_impact,
    get_file_impact,
    get_file_impact_scope,
    get_verify_commands,
    grounding_info,
    quality_check,
    set_file_impact,
    set_verify_commands,
    summary,
    validate,
)
from rebar._lib_ops import (
    bridge_check_access,
    bridge_fsck,
    bridge_pause,
    bridge_preview,
    bridge_projects_list,
    bridge_projects_remove,
    bridge_projects_set,
    bridge_resume,
    bridge_run,
    bridge_status,
    bridge_sync,
)
from rebar._lib_ops import (
    get_workflow_result as get_workflow_result,
)
from rebar._lib_ops import (
    get_workflow_status as get_workflow_status,
)
from rebar._lib_ops import (
    run_workflow as run_workflow,
)
from rebar._lib_reads import (
    _json_or as _json_or,
)
from rebar._lib_reads import (
    deps,
    export_tickets,
    fsck,
    fsck_report,
    identity_email,
    import_tickets,
    is_placeholder,
    jira_account_id,
    list_tickets,
    next_batch,
    ready,
    recent_session_logs,
    resolve_mapping,
    search,
    show_ticket,
)
from rebar._lib_warn import CrossSessionWarning

# Preserve the public ``rebar.<name>`` signatures. Redundant aliases mark deliberate
# re-exports, including the private compatibility helpers.
from rebar._lib_writes import (
    _python_leaf as _python_leaf,
)
from rebar._lib_writes import (
    add_identity_key,
    append_session_log,
    archive,
    attach_commits,
    claim,
    comment,
    compact,
    create_identity,
    create_ticket,
    edit_ticket,
    ensure_identity_for,
    idea,
    init_repo,
    link,
    reopen,
    resolve_current_identity,
    revoke_identity_key,
    sign_manifest,
    start_session_log,
    tag,
    transition,
    unlink,
    untag,
    use_identity,
    verify_signature,
)

# Native read re-exports (in-process, no subprocess).
from rebar._native import (
    apply_ticket_filters,
    find_inbound_relationships,
    reduce_all_tickets,
    reduce_ticket,
    to_llm,
)

# Best-effort ticket pushes may be hidden by the library NullHandler. Expose their
# durable, subprocess-free status so embedders can check delivery after writes.
from rebar._store.push_state import read_status as push_status

# Imports stay quiet; entrypoints replace this with the configured stderr handler.
logging.getLogger("rebar").addHandler(logging.NullHandler())

try:
    # Single source of truth: derive the version from the installed package
    # metadata so it can never drift from the distribution version.
    __version__ = importlib.metadata.version("nava-rebar")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover - dev checkout
    # Not installed (e.g. running straight from a source tree without an editable
    # install). Fall back to a sentinel rather than crashing import.
    __version__ = "0+unknown"


__all__ = [
    "KNOWN_ERROR_CODES",
    "ConcurrencyError",
    "ConfigError",
    "CrossSessionWarning",
    # exceptions
    "RebarError",
    "__version__",
    "add_identity_key",
    "append_session_log",
    "apply_ticket_filters",
    "archive",
    "attach_commits",
    "bridge_check_access",
    "bridge_fsck",
    "bridge_pause",
    "bridge_preview",
    "bridge_projects_list",
    "bridge_projects_remove",
    "bridge_projects_set",
    "bridge_resume",
    "bridge_run",
    "bridge_status",
    "bridge_sync",
    "check_ac",
    "claim",
    # quality gates + file-impact
    "clarity_check",
    "comment",
    "compact",
    "config",
    "create_identity",
    "create_ticket",
    "declare_no_file_impact",
    "deps",
    "edit_ticket",
    "engine_dir",
    "ensure_identity_for",
    "error_code_for",
    "export_tickets",
    "find_inbound_relationships",
    "fsck",
    "fsck_report",
    "get_file_impact",
    "get_file_impact_scope",
    "get_verify_commands",
    # code-grounding oracle (epic 8f6c)
    "grounding_info",
    "idea",
    "identity_email",
    "import_tickets",
    # write path
    "init_repo",
    "is_placeholder",
    "jira_account_id",
    "link",
    "list_tickets",
    "next_batch",
    "push_status",
    "quality_check",
    "ready",
    "recent_session_logs",
    # native re-exports
    "reduce_all_tickets",
    "reduce_ticket",
    "reopen",
    "resolve_current_identity",
    "resolve_mapping",
    "revoke_identity_key",
    "search",
    "set_file_impact",
    "set_verify_commands",
    # read path
    "show_ticket",
    # cryptographic manifest signing
    "sign_manifest",
    "start_session_log",
    "summary",
    "tag",
    "to_llm",
    "transition",
    "unlink",
    "untag",
    "use_identity",
    "validate",
    "verify_signature",
]
