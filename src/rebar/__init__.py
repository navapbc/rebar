"""rebar — event-sourced ticket system with a Jira reconciler.

Three interfaces over one implementation:
  * CLI:     the ``rebar`` console script (rebar.cli)
  * Library: this package — in-process reads and writes over the git-backed store
  * MCP:     the ``rebar-mcp`` console script (rebar.mcp_server)

Ticket reads and writes run in-process against the event-sourced store (the Jira
reconciler runs as a subprocess). The reducer and graph APIs (``rebar.reducer`` /
``rebar.graph``) are re-exported for callers that want in-process bulk reads.

This module is a **thin public-API namespace** (ticket S3 / 4532): the wrapper
bodies live in topical ``_lib_*`` submodules and are re-exported here, so the
``rebar`` import surface is unchanged while each unit stays under the module-size
cap. The split (all under the cap):
  * ``rebar._lib_writes`` — lifecycle + mutations + signing (holds ``_python_leaf``)
  * ``rebar._lib_gates``  — quality gates, file-impact/verify-commands, grounding
  * ``rebar._lib_reads``  — queries, export/import, fsck (holds ``_json_or``)
  * ``rebar._lib_ops``    — workflow runs, Jira reconcile, bridge-mapping audit
"""

from __future__ import annotations

import importlib.metadata
import logging

from rebar import config
from rebar._engine import engine_dir

# Exception types live in the stdlib-only leaf ``rebar._errors`` (item 9.3) so readers
# such as ``rebar._reads`` can source them downward instead of reaching UP into this
# facade. Re-exported here for back-compat: ``rebar.RebarError`` /
# ``from rebar import RebarError`` (and ``ConcurrencyError``) are unchanged.
from rebar._errors import ConcurrencyError, RebarError
from rebar._lib_gates import (
    check_ac,
    clarity_check,
    get_file_impact,
    get_verify_commands,
    grounding_info,
    quality_check,
    set_file_impact,
    set_verify_commands,
    summary,
    validate,
)
from rebar._lib_ops import (
    bridge_fsck,
    reconcile,
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
    "__version__",
    "engine_dir",
    "config",
    # exceptions
    "RebarError",
    "ConcurrencyError",
    # write path
    "init_repo",
    "create_ticket",
    "create_identity",
    "add_identity_key",
    "revoke_identity_key",
    "use_identity",
    "resolve_current_identity",
    "resolve_mapping",
    "ensure_identity_for",
    "is_placeholder",
    "jira_account_id",
    "identity_email",
    "idea",
    "transition",
    "claim",
    "reopen",
    "comment",
    "append_session_log",
    "start_session_log",
    "edit_ticket",
    "attach_commits",
    "link",
    "unlink",
    "tag",
    "untag",
    "archive",
    "compact",
    "fsck",
    "summary",
    "bridge_fsck",
    # quality gates + file-impact
    "clarity_check",
    "check_ac",
    "quality_check",
    "validate",
    "get_file_impact",
    "set_file_impact",
    "get_verify_commands",
    "set_verify_commands",
    # code-grounding oracle (epic 8f6c)
    "grounding_info",
    # cryptographic manifest signing
    "sign_manifest",
    "verify_signature",
    # read path
    "show_ticket",
    "export_tickets",
    "import_tickets",
    "list_tickets",
    "deps",
    "ready",
    "next_batch",
    "search",
    "recent_session_logs",
    # reconciler
    "reconcile",
    # native re-exports
    "reduce_all_tickets",
    "reduce_ticket",
    "to_llm",
    "find_inbound_relationships",
    "apply_ticket_filters",
]
