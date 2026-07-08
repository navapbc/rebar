"""rebar library — quality gates, file-impact / verify-commands, grounding.

The per-ticket quality-gate wrappers (``clarity_check`` / ``check_ac`` /
``quality_check``), the repo-wide ``validate``, the file-impact and verify-command
get/set pairs, the ``grounding_info`` static contract, and the per-ticket
``summary`` — split out of the ``rebar`` package facade (``__init__.py``, ticket
S3 / 4532) so it stays a thin re-export namespace. Every function is re-exported
as ``rebar.<name>``. The two ``set_*`` writers reuse ``_python_leaf`` from the
sibling write module (a one-way import; no cycle).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from rebar import config
from rebar._errors import RebarError
from rebar._lib_writes import _python_leaf

if TYPE_CHECKING:
    # Schema-derived return types (story 3a10). Import-only under TYPE_CHECKING.
    from rebar.types import (
        ClarityResult,
        FileImpactEntry,
        GateResult,
        GroundingInfo,
        ValidateReport,
        VerifyCommandEntry,
    )


# ── Quality gates + file-impact (WS5d; CLI-parity + MCP surface) ──────────────
# Quality checks exit 0=pass / 1=fail (not an error), so they report a `passed`
# boolean rather than raising.
def clarity_check(ticket_id: str, *, repo_root=None) -> ClarityResult:
    """Score ticket clarity → {score, verdict, threshold, passed}."""
    import os as _os

    from rebar._engine_support import gates, reads
    from rebar._engine_support.reads import ReadError

    tracker = reads.tracker_dir(repo_root)
    reads.ensure_fresh(tracker)
    try:
        state = reads.show_state(ticket_id, tracker)
    except ReadError as exc:
        # Schema-conformant structured failure (threshold 0 == "not evaluated").
        # ``reason``/``passed`` are library-added keys (open shape) beyond the base schema.
        return cast(
            "ClarityResult",
            {"score": 0, "verdict": "fail", "threshold": 0, "reason": str(exc), "passed": False},
        )
    threshold = gates._clarity_threshold(_os.path.dirname(tracker), None)
    data, code = gates.clarity_check_compute(
        (state.get("ticket_type") or "").strip(), state.get("description") or "", threshold
    )
    data["passed"] = code == 0
    return cast("ClarityResult", data)


def check_ac(ticket_id: str, *, repo_root=None) -> GateResult:
    """Check a ticket has an Acceptance Criteria block.

    Returns the engine's structured gate result {verdict, criteria_count, reason}
    plus a convenience ``passed`` boolean (verdict == 'pass')."""
    from rebar._engine_support import gates, reads

    tracker = reads.tracker_dir(repo_root)
    reads.ensure_fresh(tracker)
    data, code = gates.check_ac_compute(ticket_id, tracker)
    data["passed"] = code == 0  # library-added convenience key (open shape)
    return cast("GateResult", data)


def quality_check(ticket_id: str, *, repo_root=None) -> GateResult:
    """Check ticket dispatch readiness.

    Returns the engine's structured gate result {verdict, line_count,
    keyword_count, ac_items, file_impact, reason} plus a convenience ``passed``
    boolean (verdict == 'pass')."""
    from rebar._engine_support import gates, reads

    tracker = reads.tracker_dir(repo_root)
    reads.ensure_fresh(tracker)
    data, code, _warn = gates.quality_check_compute(ticket_id, tracker)
    data["passed"] = code == 0  # library-added convenience key (open shape)
    return cast("GateResult", data)


def validate(*, repo_root=None) -> ValidateReport:
    """Repo-wide quality health check (JSON report).

    ``validate`` is repo-wide and takes no ticket id. Its exit code is
    score-encoded (exit == 5 - score), so a nonzero exit is NORMAL — not a
    failure. We use the non-raising :func:`_run` and json-parse stdout,
    returning {score, critical_issues, major_issues, minor_issues, warnings,
    suggestions}.
    """
    from rebar._engine_support import validate as _validate

    tracker = str(config.tracker_dir(repo_root))
    return cast("ValidateReport", _validate.validate_state(tracker))


def get_file_impact(ticket_id: str, *, repo_root=None) -> list[FileImpactEntry]:
    """Get the current file-impact array for a ticket ([] on a miss)."""
    from rebar._engine_support import field_reads, reads

    tracker = reads.tracker_dir(repo_root)
    reads.ensure_fresh(tracker)
    return field_reads.file_impact(ticket_id, tracker)


def set_file_impact(ticket_id: str, impact, *, repo_root=None) -> None:
    """Record file impact (list of {path, reason} dicts, or a JSON string)."""
    import json as _json

    payload = impact if isinstance(impact, str) else _json.dumps(impact)
    from rebar._commands import leaf

    _python_leaf(
        leaf.set_file_impact, ticket_id, payload, repo_root=repo_root, what="set-file-impact"
    )


def get_verify_commands(ticket_id: str, *, repo_root=None) -> list[VerifyCommandEntry]:
    """Get the current DD-level verify-commands array for a ticket.

    A missing ticket raises ``RebarError`` (the dispatcher's exit-1 contract),
    unlike :func:`get_file_impact` which returns ``[]`` on a miss.
    """
    from rebar._engine_support import field_reads, reads
    from rebar._engine_support.reads import ReadError

    tracker = reads.tracker_dir(repo_root)
    reads.ensure_fresh(tracker)
    try:
        return field_reads.verify_commands(ticket_id, tracker)
    except ReadError as exc:
        raise RebarError(
            f"get-verify-commands failed (exit 1): {exc}", returncode=1, stderr=str(exc)
        ) from None


def set_verify_commands(ticket_id: str, commands, *, repo_root=None) -> None:
    """Record DD-level verify commands (list of {dd_id, dd_text, command} dicts,
    or a JSON string)."""
    import json as _json

    payload = commands if isinstance(commands, str) else _json.dumps(commands)
    from rebar._commands import leaf

    _python_leaf(
        leaf.set_verify_commands,
        ticket_id,
        payload,
        repo_root=repo_root,
        what="set-verify-commands",
    )


def grounding_info() -> GroundingInfo:
    """Return the STATIC code-grounding oracle integration contract (epic 8f6c).

    A fast, deterministic, repo-INDEPENDENT read: the closed dimension-ID
    vocabulary + version, the reference kinds, the closed abstain-reason enum (and
    the outcome/job/tier vocabularies), and the available oracle backends with
    their detected availability/version. A discovery surface for the oracle's
    consumers (5fd2/9da1); conforms to the ``grounding_info`` schema. No repo is
    scanned — only fail-open tool-version probes run.
    """
    from rebar.grounding import oracle

    return cast("GroundingInfo", oracle.contract())


def summary(*ticket_ids: str, repo_root=None) -> list[dict[str, Any]]:
    """One-line-per-ticket summary as structured JSON: a list of
    {ticket_id, status, title, blocking_summary}."""
    from rebar._engine_support import gates, reads

    tracker = reads.tracker_dir(repo_root)
    reads.ensure_fresh(tracker)
    return [gates.summary_compute(tid, tracker) for tid in ticket_ids]
