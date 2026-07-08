"""invariant_sink.py — the remediation SINK for the reconciler's structural invariants.

Detection (``invariants.detect_at_most_one_local_id`` and friends) is now a set of
PURE, I/O-free functions that return structured violation records. This module is the
counterpart: it CONSUMES those records and performs every side effect — the dedup
gate + per-pass cap, the ``alert_store`` append, and the ticket-CLI bug-filing via a
subprocess call — so detection stays unit-testable without a CLI/subprocess.

Dependency injection (why the sink takes ``runner`` / ``alert_store`` /
``extract_ticket_id`` / ``resolve_ticket_cli`` rather than importing them):
``invariants.py`` is loaded standalone in tests via ``spec_from_file_location`` and its
tests patch the ``subprocess`` name (and ``_load_alert_store``) *on the invariants
module*. The invariants wrappers therefore resolve those collaborators from their own
module namespace at call time and pass them in here, so BOTH patch styles keep working:
``patch.object(invariants.subprocess, "run", …)`` (patches the shared subprocess module)
AND ``patch.object(invariants, "subprocess")`` (rebinds the name to a MagicMock). The
sink never imports ``subprocess`` itself, so the latter style still intercepts it.

Best-effort error semantics are preserved verbatim: subprocess/OS failures during
bug-filing are surfaced to stderr but NEVER abort the loop or re-raise; genuine
programming errors (AttributeError, TypeError) still propagate.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path


class _AlertStore(Protocol):
    """The subset of the alert_store module surface the sink relies on."""

    def is_deduped(self, key: str, repo_root: Any) -> bool: ...

    def append(self, record: dict, repo_root: Any) -> Any: ...

    def patch_bug_filed(self, dedup_key: str, bug_id: str, repo_root: Any) -> Any: ...


def _file_at_most_one_bug(
    runner: Any,
    ticket_cli: str,
    jira_key: str,
    extract_ticket_id: Callable[[str], str],
) -> tuple[str, str | None]:
    """Shell out to file ONE at-most-one bug ticket, returning ``(bug_id, cli_error)``.

    ``runner`` is a subprocess-like module (the real ``subprocess`` module, or a
    test double). Narrow exception handling: catch the subprocess-class and OS-class
    exceptions explicitly so genuine programming errors (AttributeError, TypeError)
    still propagate rather than being silently swallowed. ``runner.TimeoutExpired`` /
    ``runner.SubprocessError`` are read off ``runner`` so a rebound test double is
    honored — matching the original module-global ``subprocess`` references.
    """
    bug_id = ""
    cli_error: str | None = None
    try:
        result = runner.run(
            [
                ticket_cli,
                "create",
                "bug",
                f"at-most-one violation: {jira_key} has multiple local_ids",
                "--priority",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            bug_id = extract_ticket_id(result.stdout)
            if not bug_id:
                # CLI exited 0 but stdout did not contain a canonical ticket
                # ID — treat as failure so the alert remains orphan-with-warning
                # instead of being patched with a garbage value.
                cli_error = (
                    f"exit=0 but no canonical ticket ID found in stdout={result.stdout[:200]!r}"
                )
        else:
            cli_error = f"exit={result.returncode} stderr={result.stderr[:200]!r}"
    except runner.TimeoutExpired:
        cli_error = "ticket-create timed out after 30s"
    except (OSError, runner.SubprocessError) as exc:
        cli_error = f"{type(exc).__name__}: {exc}"
    return bug_id, cli_error


def file_at_most_one_violations(
    violations: list,
    *,
    repo_root: Path,
    ticket_cli: str,
    alert_store: _AlertStore,
    runner: Any,
    extract_ticket_id: Callable[[str], str],
    cap: int,
) -> list[dict]:
    """Remediate at-most-one-local-id violations: dedup + cap, alert append, bug filing.

    ``violations`` is the list of structured violation records produced by
    ``invariants.detect_at_most_one_local_id`` (each exposes ``jira_key``,
    ``local_ids``, ``dedup_key``, ``legacy_dedup_key``). Returns the list of filed
    violation dicts this pass (``{jira_key, local_ids, dedup_key}``), preserving the
    original ``check_at_most_one_local_id`` return shape and ordering.

    Cap semantics preserved: the per-pass cap counts only FILED (non-deduped)
    violations and is checked before the dedup gate — so deduped violations never
    count against the cap.

    Bug-filing failures (TimeoutExpired, FileNotFoundError, OSError) are surfaced to
    stderr but do NOT abort the loop or re-raise — the alert record itself is left on
    disk so the next reconciler pass re-files cleanly (or is short-circuited by
    is_deduped on the persisted record).
    """
    violations_filed: list[dict] = []
    for v in violations:
        if len(violations_filed) >= cap:
            continue

        # Backward-compat: also check the legacy (pre-prefix) dedup_key so alerts
        # filed under the old format are recognized during the transition window
        # and not re-filed as duplicates.
        if alert_store.is_deduped(v.dedup_key, repo_root) or alert_store.is_deduped(
            v.legacy_dedup_key, repo_root
        ):
            continue

        # File bridge-alert record before filing the ticket so subsequent passes
        # hit is_deduped() and skip.
        record = {
            "key": v.dedup_key,
            "jira_key": v.jira_key,
            "timestamp_ns": time.time_ns(),
            "reason": f"multiple local_ids: {v.local_ids}",
        }
        alert_store.append(record, repo_root)

        bug_id, cli_error = _file_at_most_one_bug(runner, ticket_cli, v.jira_key, extract_ticket_id)

        if bug_id:
            alert_store.patch_bug_filed(v.dedup_key, bug_id, repo_root)
        else:
            # Bug-filing failed. Surface the failure to operators via stderr, then
            # leave the alert record on disk. The next pass will hit is_deduped()
            # and skip — operators must manually file the bug or roll the alert
            # forward.
            print(  # noqa: T201
                f"WARN: invariants.check_at_most_one_local_id: "
                f"alert {v.dedup_key!r} filed but bug-ticket creation "
                f"failed ({cli_error}); alert is orphan-without-bug.",
                file=sys.stderr,
            )

        violations_filed.append(
            {
                "jira_key": v.jira_key,
                "local_ids": v.local_ids,
                "dedup_key": v.dedup_key,
            }
        )

    return violations_filed


def report_schema_drift(
    issue_key: str,
    observed: dict,
    expected: dict,
    *,
    repo_root: Path,
    alert_store: _AlertStore,
    runner: Any,
    resolve_ticket_cli: Callable[[], str],
) -> None:
    """File a dedup'd bug ticket for schema drift (best-effort side-effect sink).

    Uses a stable ``dedup_key`` of the form ``bridge-alert:schema-drift:<issue_key>``
    so repeated drift on the same issue can be correlated. The alert_store dedup check
    prevents duplicate tickets across reconciler passes — without it, every pass that
    hits the cap fires a new ticket for the same key.

    The alert record is persisted BEFORE filing the ticket so subsequent passes hit
    is_deduped() and skip, mirroring ``file_at_most_one_violations``. The ticket CLI
    is resolved lazily (only after the dedup gate passes) so a deduped call does no
    extra work. Subprocess failures are swallowed (``check=False``) — drift reporting
    is best-effort and must not abort the reconcile loop.
    """
    dedup_key = f"bridge-alert:schema-drift:{issue_key}"

    if alert_store.is_deduped(dedup_key, repo_root):
        return

    alert_store.append(
        {
            "key": dedup_key,
            "issue_key": issue_key,
            "timestamp_ns": time.time_ns(),
            "reason": f"schema-drift observed={observed} expected={expected}",
        },
        repo_root,
    )

    ticket_cli = resolve_ticket_cli()
    runner.run(
        [
            ticket_cli,
            "create",
            "bug",
            f"schema drift: {issue_key}",
            "--priority",
            "2",
            "--description",
            f"dedup_key={dedup_key} observed={observed} expected={expected}",
        ],
        check=False,
    )
