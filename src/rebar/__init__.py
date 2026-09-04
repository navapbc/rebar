"""rebar — event-sourced ticket system with a Jira reconciler.

Three interfaces over one implementation:
  * CLI:     the ``rebar`` console script (rebar.cli)
  * Library: this package — in-process reads and writes over the git-backed store
  * MCP:     the ``rebar-mcp`` console script (rebar.mcp_server)

Ticket reads, writes, and explicit bridge operations run through the package facade.
The reducer and graph APIs (``rebar.reducer`` / ``rebar.graph``) are re-exported
for callers that want in-process bulk reads.

This module is a **thin public-API namespace** (ticket S3 / 4532): the wrapper
bodies live in topical ``_lib_*`` submodules and are re-exported here, so the
``rebar`` import surface is unchanged while each unit stays under the module-size
cap. The split (all under the cap):
  * ``rebar._lib_writes`` — lifecycle + mutations + signing (holds ``_python_leaf``)
  * ``rebar._lib_gates``  — quality gates, file-impact/verify-commands, grounding
  * ``rebar._lib_reads``  — queries, export/import, fsck (holds ``_json_or``)
  * ``rebar._lib_ops``    — workflow runs, explicit bridge ops, bridge-mapping audit
"""

from __future__ import annotations

import importlib.metadata
import logging

from rebar import config

# The config-fault exception (raised when rebar.toml cannot be read while resolving a
# verify.* gate — operator ruling 39f8-ae7c: an unreadable config is an ERROR, not a
# silent fall-back to the gate's default). Re-exported so library callers can catch
# ``rebar.ConfigError`` next to ``rebar.RebarError`` without importing rebar.config.
from rebar._config_coercion import ConfigError
from rebar._engine import engine_dir

# Exception types live in the stdlib-only leaf ``rebar._errors`` (item 9.3) so readers
# such as ``rebar._reads`` can source them downward instead of reaching UP into this
# facade. Re-exported here for back-compat: ``rebar.RebarError`` /
# ``from rebar import RebarError`` (and ``ConcurrencyError``) are unchanged.
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

# ── Public API re-exports (thin facade over the topical ``_lib_*`` submodules) ──
# Every name below stays importable as ``rebar.<name>`` with its identical
# signature. The private helpers ``_python_leaf`` / ``_json_or`` are re-exported
# too (redundant ``as`` aliases mark them as deliberate re-exports for the linter).
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

# Bug vapoury-attack-lamb: the tickets-branch push is best-effort and warns on failure —
# but a library embedder gets the NullHandler installed below, so that warning goes
# nowhere and a rejected push is invisible in-process. This is the read side of the
# durable marker ``rebar._store.push_state`` records: it needs no logging handler and no
# git subprocess, so an embedder can check delivery after a write.
from rebar._store.push_state import read_status as push_status

# Library hygiene — quiet by default. Attach a NullHandler to the ``rebar`` root logger
# so importing rebar as a library never emits to stderr or warns about a missing
# handler. Entrypoints install a real stderr handler via
# ``rebar._logging.install_stderr_handler``. See ``rebar._logging`` for the convention.
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
