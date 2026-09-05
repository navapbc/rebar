"""Held-out oracle for removing internal-only compatibility shims (ticket e54e)."""

from __future__ import annotations

from rebar._commands import close_precheck, transition_close
from rebar._store import event_append, event_commit_git, gitutil

_OUTPUT_MODEL_NAMES = (
    "BridgeAccessCheckOut",
    "BridgeControlOut",
    "BridgeFsckOut",
    "BridgeRunOut",
    "BridgeStatusOut",
    "ClaimResultOut",
    "ClarityResultOut",
    "CreateResultOut",
    "DepsGraphOut",
    "FileImpactItemOut",
    "FsckOut",
    "GateResultOut",
    "GroundingBackendOut",
    "GroundingInfoOut",
    "NextBatchOut",
    "SignResultOut",
    "TicketStateOut",
    "ValidateReportOut",
    "VerifyCommandItemOut",
    "VerifySignatureResultOut",
    "WorkflowRunOut",
)

_TRANSIENT_INDEX_WRITE_STDERR = "fatal: unable to write new index file"
_POST_REF_INDEX_WRITE_STDERR = (
    "fatal: repository has been updated, but unable to write\nnew_index file."
)


def test_mcp_server_no_longer_reexports_output_models() -> None:
    import rebar._mcp_models as models
    import rebar.mcp_server as mcp_server

    missing = [name for name in _OUTPUT_MODEL_NAMES if not hasattr(models, name)]
    leaked = [name for name in _OUTPUT_MODEL_NAMES if hasattr(mcp_server, name)]

    assert missing == [] and leaked == []


def test_transient_write_classifier_has_only_the_canonical_gitutil_name() -> None:
    leaked = [
        module.__name__
        for module in (event_append, event_commit_git)
        if hasattr(module, "_is_transient_add_error")
    ]

    assert leaked == []
    assert gitutil._is_transient_object_write_error(_TRANSIENT_INDEX_WRITE_STDERR)
    assert not gitutil._is_transient_object_write_error(_POST_REF_INDEX_WRITE_STDERR)


def test_close_precheck_exposes_referencing_scan_without_bool_wrapper(monkeypatch) -> None:
    monkeypatch.setattr(close_precheck, "_referencing_commits", lambda *a, **k: ["abc"])

    assert close_precheck._referencing_commits({"t"}, "/tracker", "/code") == ["abc"]
    assert not hasattr(close_precheck, "_referencing_commit_exists")
    assert not hasattr(transition_close, "_referencing_commit_exists")
