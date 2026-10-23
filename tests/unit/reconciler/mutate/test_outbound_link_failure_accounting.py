"""A failed outbound link operation must not count as applied. These tests distinguish
confirmed idempotent removal from unproven ACLI failures and preserve visible error
reporting. Both link subcommands pass --yes to avoid interactive cancellation."""

from __future__ import annotations

import importlib
import io
import subprocess
from contextlib import redirect_stderr
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
_ENGINE = REPO_ROOT / "src" / "rebar" / "reconciler"


@pytest.fixture(scope="module")
def batch_dispatch() -> ModuleType:
    return importlib.import_module("rebar.reconciler.batch_dispatch")


@pytest.fixture(scope="module")
def applier_mod() -> ModuleType:
    return importlib.import_module("rebar.reconciler.applier")


class _LinkClient:
    """Minimal outbound transport: one existing link, scripted failures per op."""

    def __init__(self, *, delete_exc=None, create_exc=None) -> None:
        self._delete_exc = delete_exc
        self._create_exc = create_exc
        self.deleted: list[str] = []
        self.created: list[tuple[str, str, str]] = []

    def update_issue(self, key, **kwargs):
        return {"key": key}

    def get_issue_links(self, key):
        return [{"id": "88", "type": {"name": "Blocks"}, "inwardIssue": {"key": "PROJ-2"}}]

    def delete_issue_link(self, link_id):
        if self._delete_exc is not None:
            raise self._delete_exc
        self.deleted.append(link_id)
        return {"status": "deleted", "link_id": link_id}

    def set_relationship(self, frm, to, link_type="Blocks"):
        if self._create_exc is not None:
            raise self._create_exc
        self.created.append((frm, to, link_type))
        return {"status": "created"}


def _remove_mutation() -> dict:
    return {
        "key": "PROJ-1",
        "fields": {},
        "links": [{"action": "remove", "type": "Blocks", "to_key": "PROJ-2"}],
    }


def _add_mutation() -> dict:
    return {
        "key": "PROJ-1",
        "fields": {},
        "links": [{"action": "add", "type": "Blocks", "to_key": "PROJ-9"}],
    }


def _cancelled(stderr: str = "✗ Error: command cancelled") -> subprocess.CalledProcessError:
    """The exact production failure: ACLI aborting on an unanswered confirmation prompt."""
    return subprocess.CalledProcessError(1, ["jira", "workitem", "link", "delete"], stderr=stderr)


# --- The accounting -------------------------------------------------------------------


def test_cancelled_delete_counts_failed_not_applied(batch_dispatch: ModuleType) -> None:
    """THE REGRESSION. A cancelled delete must not be reported as an applied link."""
    client = _LinkClient(delete_exc=_cancelled())
    subop: dict[str, int] = {}
    with redirect_stderr(io.StringIO()):
        batch_dispatch.update_one(_remove_mutation(), client, subop_applied=subop)
    assert subop.get("links_applied") == 0, "a cancelled delete removed nothing"
    assert subop.get("links_failed") == 1
    assert subop.get("links_computed") == 1, "the remove was still attempted"


def test_cancelled_delete_is_still_non_fatal(batch_dispatch: ModuleType) -> None:
    """Counting the failure must not start unwinding the batch — the scalar update stands."""
    client = _LinkClient(delete_exc=_cancelled())
    with redirect_stderr(io.StringIO()):
        batch_dispatch.update_one(_remove_mutation(), client, subop_applied={})


def test_cancelled_delete_is_logged(batch_dispatch: ModuleType) -> None:
    """The old arm was silent, which is why this went unseen for six runs."""
    buf = io.StringIO()
    client = _LinkClient(delete_exc=_cancelled())
    with redirect_stderr(buf):
        batch_dispatch.update_one(_remove_mutation(), client, subop_applied={})
    assert "delete_issue_link failed" in buf.getvalue()


@pytest.mark.parametrize("marker", ["404", "409", "does not exist", "Not Found"])
def test_proven_gone_delete_counts_idempotent(batch_dispatch: ModuleType, marker: str) -> None:
    """A delete whose stderr proves the link is already gone DID reach the end-state."""
    client = _LinkClient(delete_exc=_cancelled(stderr=f"Error: {marker}"))
    subop: dict[str, int] = {}
    with redirect_stderr(io.StringIO()):
        batch_dispatch.update_one(_remove_mutation(), client, subop_applied=subop)
    assert subop.get("links_applied") == 1, f"{marker!r} proves the link is gone"
    assert subop.get("links_failed") == 0


def test_proven_gone_on_stdout_counts_idempotent(batch_dispatch: ModuleType) -> None:
    """The marker may arrive on STDOUT, not stderr, and still proves the link is gone.

    ``workitem link delete`` runs WITHOUT ``--json`` (story 25ae), so its failure output is
    human-readable text on the exit-code path and ACLI can write the "404 / not found" line to
    stdout. ``delete_issue`` in adapters/jira/acli.py already reads both streams for exactly
    this classification; a stderr-only check here would score a genuine concurrent-removal
    race as a failure, inflating links_failed and firing the canary on a benign race.
    """
    exc = subprocess.CalledProcessError(1, ["jira", "workitem", "link", "delete"], stderr="")
    exc.stdout = "Error: 404 not found"
    client = _LinkClient(delete_exc=exc)
    subop: dict[str, int] = {}
    with redirect_stderr(io.StringIO()):
        batch_dispatch.update_one(_remove_mutation(), client, subop_applied=subop)
    assert subop.get("links_applied") == 1, "a stdout-carried 404 still proves the link is gone"
    assert subop.get("links_failed") == 0


def test_delete_without_stderr_counts_failed(batch_dispatch: ModuleType) -> None:
    """Absent stderr proves nothing, so it must NOT inherit the idempotent benefit."""
    client = _LinkClient(
        delete_exc=subprocess.CalledProcessError(1, ["jira", "workitem", "link", "delete"])
    )
    subop: dict[str, int] = {}
    with redirect_stderr(io.StringIO()):
        batch_dispatch.update_one(_remove_mutation(), client, subop_applied=subop)
    assert subop.get("links_applied") == 0
    assert subop.get("links_failed") == 1


def test_non_acli_delete_failure_counts_failed(batch_dispatch: ModuleType) -> None:
    """The Data Center path raises HTTPError, not CalledProcessError — also counted."""
    client = _LinkClient(delete_exc=RuntimeError("DC boom"))
    subop: dict[str, int] = {}
    with redirect_stderr(io.StringIO()):
        batch_dispatch.update_one(_remove_mutation(), client, subop_applied=subop)
    assert subop.get("links_applied") == 0
    assert subop.get("links_failed") == 1


def test_create_failure_counts_failed(batch_dispatch: ModuleType) -> None:
    """The ADD side was never miscounted, but it was uncounted — now surfaced too."""
    client = _LinkClient(create_exc=RuntimeError("create boom"))
    subop: dict[str, int] = {}
    with redirect_stderr(io.StringIO()):
        batch_dispatch.update_one(_add_mutation(), client, subop_applied=subop)
    assert subop.get("links_applied") == 0
    assert subop.get("links_failed") == 1


def test_success_reports_zero_failed(batch_dispatch: ModuleType) -> None:
    """The happy path keeps its existing meaning and adds an explicit zero."""
    client = _LinkClient()
    subop: dict[str, int] = {}
    batch_dispatch.update_one(_remove_mutation(), client, subop_applied=subop)
    assert subop.get("links_applied") == 1
    assert subop.get("links_failed") == 0
    assert client.deleted == ["88"]


# --- The operator-visible surface -----------------------------------------------------


def test_batch_outcome_line_carries_links_failed(applier_mod: ModuleType) -> None:
    """``links_failed`` must appear in the same line operators already read."""
    buf = io.StringIO()
    outcome = {"key": "PROJ-1", "error": None, "links_applied": 0, "links_failed": 2}
    with redirect_stderr(buf):
        applier_mod._print_batch_recon("update", outcome, soft_failed=False)
    line = buf.getvalue()
    assert "links_failed=2" in line
    assert "links_applied=0" in line


# --- The root cause -------------------------------------------------------------------


class _ArgvCapturingGraph:
    """AcliGraphMixin with the transport replaced by an argv recorder."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def _run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture(scope="module")
def graph_cls():
    from rebar.reconciler.adapters.jira.acli_graph import AcliGraphMixin

    return type("_Graph", (_ArgvCapturingGraph, AcliGraphMixin), {})


def test_link_create_passes_yes(graph_cls) -> None:
    """Without --yes the subcommand prompts, reads EOF, and aborts 'command cancelled'."""
    graph = graph_cls()
    graph.set_relationship("PROJ-1", "PROJ-2", "Blocks")
    assert graph.calls, "set_relationship must shell out"
    assert "--yes" in graph.calls[0], graph.calls[0]


def test_link_delete_passes_yes(graph_cls) -> None:
    """The delete path is the one that produced the miscounted failures in production."""
    graph = graph_cls()
    graph.delete_issue_link("88")
    assert graph.calls, "delete_issue_link must shell out"
    assert "--yes" in graph.calls[0], graph.calls[0]


def test_link_subcommands_still_omit_json(graph_cls) -> None:
    """Story 25ae: the installed ACLI rejects --json here. --yes must not smuggle it back."""
    graph = graph_cls()
    graph.set_relationship("PROJ-1", "PROJ-2", "Blocks")
    graph.delete_issue_link("88")
    for cmd in graph.calls:
        assert "--json" not in cmd, cmd
