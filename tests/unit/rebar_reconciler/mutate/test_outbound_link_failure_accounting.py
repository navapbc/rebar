"""Outbound link ops must never score a failure as an applied link (ticket 5528).

WHY THIS MODULE EXISTS. The outbound link-apply loop treated EVERY
``subprocess.CalledProcessError`` from ``delete_issue_link`` as an idempotent
concurrent-removal race and incremented ``links_applied``. That arm carried no print, so a
failing delete left no trace anywhere except ``_run_acli``'s retry-exhausted ``ACLI stderr:``
line — and the enclosing ``RECON: batch_outcome`` reported ``error=None`` with a nonzero
``links_applied``.

The measured consequence, from the evidence pass recorded on ticket 5528: across one failed
Reconcile Bridge run there were 15 occurrences of ``ACLI stderr: X Error: command cancelled``,
a summed ``links_applied`` of exactly 15 for the whole pass, and zero
``update_one: ... failed`` lines. The correspondence held per ticket (2 cancellations ->
``REB-1305 links_applied=2``; 1 -> ``REB-1308 links_applied=1``). Every reported link
application in that pass was a failure wearing a success's clothes, and no link was actually
removed. The run ids and the per-ticket tallies live on ticket 5528, which owns that history.

The cause was deterministic rather than a race: ``workitem link create`` / ``link delete``
prompt for confirmation, ``_run_acli`` spawns with ``stdin=subprocess.DEVNULL``, so the prompt
read EOF and ACLI aborted with "command cancelled" on every pass forever. The arm's own
"self-healing — the differ recomputes the REMOVE next pass" rationale is only true for a
TRANSIENT failure, so it never healed.

This is the bug-44de silent-divergence class, and ``REBAR_RECONCILER_FAIL_SILENT_NOOP`` was
structurally blind to it: that canary fires on ``computed > 0 and applied == 0``, and the arm
inflated ``applied`` to equal ``computed``.

WHAT IS PINNED HERE. Two independent guarantees, because either alone leaves the hole open:
  * the ACCOUNTING — only a proven-gone link counts applied; everything else counts failed and
    is surfaced, so the canary can see it (``test_cancelled_*``, ``test_*_idempotent_*``);
  * the ROOT CAUSE — ``--yes`` is present on both link subcommands so the prompt never
    happens (``test_link_*_passes_yes``).

DELIBERATE CHOICE. The argv assertions below spell ``--yes`` as a literal rather than
importing any constant from the adapter, so the oracle cannot move with the code under test.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stderr
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
_ENGINE = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def batch_dispatch() -> ModuleType:
    return _load("batch_dispatch_5528", _ENGINE / "batch_dispatch.py")


@pytest.fixture(scope="module")
def applier_mod() -> ModuleType:
    return _load("applier_5528", _ENGINE / "applier.py")


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
    from rebar_reconciler.adapters.jira.acli_graph import AcliGraphMixin

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
