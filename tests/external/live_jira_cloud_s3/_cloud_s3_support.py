"""Shared constants + read-only helpers for the Cloud+S3 rehearsal.

Self-contained sibling module (mirrors ``tests/external/live_jira_dc/_dc_support``):
pytest's default ``prepend`` import mode puts this directory on ``sys.path`` when the
suite is collected, so ``conftest.py`` and the test module both import these names
with a bare ``from _cloud_s3_support import ...`` — never ``from conftest import``
(the repo root's conftest also registers under the bare name ``conftest``, a
documented collision hazard).

Everything here is READ-ONLY with respect to Jira: the only Jira calls are searches
and the transport constructor. The mutating-method inventory drives the structural
guard in ``conftest.py``; it lists what to FORBID, it does not call any of them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import rebar

# ---------------------------------------------------------------------------
# Projects + repo configs the rehearsal maps
# ---------------------------------------------------------------------------

#: The two real Jira Cloud projects this rehearsal drives.
REB_PROJECT = "REB"
DIG_PROJECT = "DIG"

#: The single-repo (REB) and two-repo (DIG) configs the mapping seeds, so the
#: single/two-repo resolution paths are both exercised by one store.
REB_REPOS = ["rebar"]
DIG_REPOS = ["rebar-web", "rebar-api"]

#: Cloud-cred env vars a live-Cloud client needs — the rehearsal REQUIRES these.
_CLOUD_CRED_VARS = ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN")

#: Default throwaway bucket (story 368f's encrypted rehearsal bucket). Overridable
#: with ``REBAR_REHEARSAL_S3_BUCKET`` so an operator can point at their own bucket.
DEFAULT_BUCKET = "rebar-rehearsal-368f-896586841071"

#: The remote name the S3 store copy is wired onto and pinned as ``sync.remote``.
REHEARSAL_REMOTE_NAME = "rehearsal-s3"

#: Every mutating method on the Cloud transport. The read-only guard patches each to
#: raise, so no scenario can drive an outbound Jira write. Read methods
#: (``search_issues``, ``get_issue``, ``get_comments``, ``get_parent_map``,
#: ``get_issuelinks_map``, ``get_server_info``, ``get_myself``) are deliberately
#: absent so the inbound fetch still works.
MUTATING_TRANSPORT_METHODS = (
    "create_issue",
    "update_issue",
    "add_comment",
    "add_label",
    "remove_label",
    "set_entity_property",
    "set_issue_property",
    "set_reporter",
    "set_relationship",
    "delete_issue",
    "transition_issue_by_name",
    "unassign_issue",
    "set_parent",
    "update_priority",
    "update_issuetype",
    "update_comment",
    "delete_comment",
    "delete_issue_link",
)


class JiraWriteForbidden(AssertionError):
    """Raised if a read-only scenario attempts ANY outbound Jira mutation."""


# ---------------------------------------------------------------------------
# Engine import + transport
# ---------------------------------------------------------------------------


def engine_on_path() -> None:
    """Put ``<repo>/src/rebar/_engine`` on ``sys.path`` so ``rebar_reconciler`` resolves.

    The reconciler ships as a stdlib-only package UNDER the wheel rather than as a
    top-level install (mirrors ``tests/external/test_link_sync_live.py`` and the DC
    harness helpers).
    """
    engine_dir = Path(rebar.__file__).resolve().parent / "_engine"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))


def transport_class() -> type:
    """The live-Cloud transport class every mutating method is patched on."""
    engine_on_path()
    from rebar_reconciler.adapters.jira import acli as mod

    return mod.AcliClient


def build_cloud_client(project: str = REB_PROJECT) -> Any:
    """A live-Cloud ``AcliClient`` from JIRA_URL/JIRA_USER/JIRA_API_TOKEN.

    Used only for READ queries here (the count oracle). Build a FRESH client for any
    before/after comparison: ``search_issues`` caches per-JQL PER instance, so a
    reused client could answer stale.
    """
    cls = transport_class()
    return cls(
        jira_url=os.environ["JIRA_URL"],
        user=os.environ["JIRA_USER"],
        api_token=os.environ["JIRA_API_TOKEN"],
        jira_project=project,
    )


def project_issue_count(project: str) -> int:
    """The number of issues a FRESH client sees in *project* (read-only oracle).

    The before/after equality of this count is the belt-and-braces complement to the
    structural guard: even if some path slipped past the guard, a changed count would
    surface it. ``search_issues`` fetches the WHOLE JQL result set but returns a page
    slice (default ``max_results=50``), so an unbounded ``max_results`` is passed to
    count the true total — a 50-capped count would read equal for any project over 50
    issues and make this oracle vacuous.
    """
    jql = f'project = "{project}"'
    return len(build_cloud_client(project).search_issues(jql, start_at=0, max_results=10**9))


# ---------------------------------------------------------------------------
# Readiness predicates (drive skips; no side effects)
# ---------------------------------------------------------------------------


def live_jira_ready() -> bool:
    """True iff live Jira creds AND the ``acli`` binary are present."""
    creds = all(os.environ.get(k) for k in _CLOUD_CRED_VARS)
    return bool(creds) and shutil.which("acli") is not None


def s3_backend_ready() -> tuple[bool, str]:
    """Return ``(ready, reason)`` for the S3 store backend precondition.

    Requires the ``git-remote-s3`` helper on PATH and a resolvable AWS identity (a
    cheap ``aws sts get-caller-identity``). Absence is a SKIP, not a failure — the
    suite has no isolated S3 store to rehearse against.
    """
    if shutil.which("git-remote-s3") is None:
        return False, "git-remote-s3 helper is not installed (uv pip install git-remote-s3)"
    if shutil.which("aws") is None:
        return False, "the aws CLI is not installed"
    who = subprocess.run(
        ["aws", "sts", "get-caller-identity"], capture_output=True, text=True, check=False
    )
    if who.returncode != 0:
        return False, f"no usable AWS identity for the S3 store backend: {who.stderr.strip()}"
    return True, ""


# ---------------------------------------------------------------------------
# git + S3 helpers
# ---------------------------------------------------------------------------


def git_run(argv: list[str], cwd: Path | str) -> subprocess.CompletedProcess[str]:
    """Run git, raising with git's OWN stderr on failure (diagnostic, not 'exit N')."""
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} failed in {cwd} (exit {result.returncode}): "
            f"{result.stderr.strip() or '<git wrote no stderr>'}"
        )
    return result


def s3_url() -> str:
    """A unique throwaway ``s3://<bucket>/<prefix>`` for this run's store."""
    explicit = os.environ.get("REBAR_REHEARSAL_S3_REMOTE", "").strip()
    if explicit:
        return explicit
    bucket = os.environ.get("REBAR_REHEARSAL_S3_BUCKET", DEFAULT_BUCKET).strip()
    prefix = f"cloud-s3-rehearsal/{uuid.uuid4().hex[:16]}"
    return f"s3://{bucket}/{prefix}"


def delete_s3_prefix(url: str) -> None:
    """Best-effort teardown: remove every object under the throwaway prefix."""
    subprocess.run(
        ["aws", "s3", "rm", url, "--recursive"], capture_output=True, text=True, check=False
    )
