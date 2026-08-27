"""Offline oracle for ``_dc_support.force_issue_reindex`` (bug 2c60, epic REB-3115).

The live rich-text DC lane flaked because Jira DC's Lucene index is eventually consistent
with an UNBOUNDED background-reindex latency (ADR 0037 §3): on the ephemeral CI instance under
load the reindex thread was starved and a just-pushed ``description`` never became visible to
the JQL SEARCH the reconcile pass reads from, even within 240s. The robust fix does not widen a
timeout — it forces a synchronous per-issue reindex through the admin
``IssueIndexingService`` REST resource so the search reflects the write deterministically.

This module pins that helper's OBSERVABLE CONTRACT harness-free, so the fix has teeth without a
1.5h live run: given a working id read it must POST to ``/rest/api/2/reindex/issue`` for that
issue's numeric id, and given a failed id read it must NOT reindex (and never raise) so the
caller degrades to its existing wait rather than crashing the lane.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_dc_support() -> Any:
    """Import ``tests/external/live_jira_dc/_dc_support.py`` BY PATH (harness-free).

    That directory is only on ``sys.path`` while pytest collects the external suite, so a plain
    ``import _dc_support`` works in a full run and fails in a unit-only one. Its import builds
    ``skip_no_harness``, which probes the harness over the network; unit tests forbid network
    access, so the probe's ``urlopen`` is stubbed to its unreachable answer for the import only.
    """
    path = _REPO_ROOT / "tests" / "external" / "live_jira_dc" / "_dc_support.py"
    spec = importlib.util.spec_from_file_location("_dc_support_for_force_reindex", path)
    assert spec and spec.loader, f"could not load the live suite's helpers from {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    real_urlopen = urllib.request.urlopen

    def _no_probe(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("harness probe suppressed: unit tests do not touch the network")

    urllib.request.urlopen = _no_probe  # type: ignore[assignment]
    try:
        spec.loader.exec_module(module)
    finally:
        urllib.request.urlopen = real_urlopen  # type: ignore[assignment]
    return module


@pytest.fixture
def support() -> Any:
    return _load_dc_support()


class _RecordingRequest:
    """A fake ``dc_request`` that records calls and replays a queued ``(status, body)``."""

    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, path: str, *, method: str = "GET", **_kwargs: Any) -> tuple[int, Any]:
        self.calls.append((path, method))
        return self._responses.pop(0)


def test_force_reindex_posts_the_issue_id_to_the_reindex_resource(support: Any) -> None:
    """The happy path: read the numeric id, then POST it to the per-issue reindex resource.

    Asserts on the SECOND call (the reindex) — its method is POST, it targets
    ``/rest/api/2/reindex/issue``, and it carries the numeric id the GET returned, not the
    issue KEY. A helper that reindexed by key, or issued a GET-only, would leave the starved
    background reindex in charge and the lane would flake exactly as observed.
    """
    req = _RecordingRequest([(200, {"id": "10042", "key": "RBJUIRI-1"}), (200, None)])

    support.force_issue_reindex(req, "RBJUIRI-1")

    assert len(req.calls) == 2, f"expected an id read then a reindex POST, got {req.calls}"
    reindex_path, reindex_method = req.calls[1]
    assert reindex_method == "POST", f"the reindex must be a POST, got {reindex_method}"
    assert "/rest/api/2/reindex/issue" in reindex_path, (
        f"the reindex must hit the per-issue IssueIndexingService resource, got {reindex_path}"
    )
    assert "issueId=10042" in reindex_path, (
        f"the reindex must name the numeric issue id (10042), not the key, got {reindex_path}"
    )
    assert "RBJUIRI-1" not in reindex_path, (
        f"the reindex must not address the issue by KEY, got {reindex_path}"
    )


def test_force_reindex_does_not_reindex_when_the_id_read_fails(support: Any) -> None:
    """A failed id read degrades to a NO-OP, never a raise.

    The reindex is an ACCELERATOR, not a new hard dependency: if the id GET returns non-200 the
    caller must fall back to its existing search wait, so this must issue no POST and not raise.
    """
    req = _RecordingRequest([(503, "upstream unavailable")])

    result = support.force_issue_reindex(req, "RBJUIRI-1")

    assert len(req.calls) == 1, f"a failed id read must not trigger a reindex POST, got {req.calls}"
    assert req.calls[0][1] == "GET"
    assert result[0] == 503, f"the failed status must be surfaced to the caller, got {result!r}"


def test_force_reindex_does_not_reindex_when_the_id_is_absent(support: Any) -> None:
    """A 200 with no ``id`` field is also a safe NO-OP (defensive against an odd payload)."""
    req = _RecordingRequest([(200, {"key": "RBJUIRI-1"})])

    support.force_issue_reindex(req, "RBJUIRI-1")

    assert len(req.calls) == 1, f"an id-less body must not trigger a reindex POST, got {req.calls}"
