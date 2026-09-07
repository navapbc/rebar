"""Verify completion prefetch reads verdict evidence from the active pinned code root.

An attested snapshot governs declared files and discovered test globs even when `repo_root` names
another checkout. Local mode continues to use the supplied root.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from rebar._snapshot.repo_snapshot import SOURCE_ATTESTED, SnapshotHandle
from rebar.llm import gate_source
from rebar.llm.gate_context import current_code_root
from rebar.llm.workflow import completion_prefetch as pf

pytestmark = pytest.mark.unit

_TICKET_ID = "rec-0000-0000-0001"
_PATH = "target.py"
_TEST_PATH = "tests/test_target.py"  # discovered via _discover_test_globs(tests/**/test_target*.py)
_LIVE_B = "LIVE_B_CONTENT = 'mutable server checkout (B)'\n"
_PINNED_A = "PINNED_A_CONTENT = 'the --ref A immutable tree'\n"
_PINNED_A_TEST = "PINNED_A_TEST = 'the --ref A test body'\n"


def _write_tree(root: Path, body: str, *, test_body: str | None) -> None:
    (root / _PATH).write_text(body)
    if test_body is not None:
        (root / "tests").mkdir()
        (root / _TEST_PATH).write_text(test_body)


def _fixture(tmp_path: Path) -> tuple[str, str]:
    """Create divergent roots with a test file present only in the pinned snapshot."""
    live_b = tmp_path / "live_checkout_B"
    snap_a = tmp_path / "pinned_snapshot_A"
    live_b.mkdir()
    snap_a.mkdir()
    _write_tree(live_b, _LIVE_B, test_body=None)
    _write_tree(snap_a, _PINNED_A, test_body=_PINNED_A_TEST)
    return str(live_b), str(snap_a)


def _stub_ticket():
    """Stub ONLY the irreducible ticket-state boundary: file_impact declares target.py."""
    fake = {"ticket_id": _TICKET_ID, "file_impact": [{"path": _PATH, "reason": "impl"}]}
    import rebar._reads as _reads

    return mock.patch.object(_reads, "show_ticket", return_value=fake)


def test_prefetch_reads_pinned_snapshot_not_live_checkout(tmp_path: Path) -> None:
    """In a --ref A attested gate, prefetch verdict evidence must resolve to A, not live B."""
    live_b, snap_a = _fixture(tmp_path)
    handle = SnapshotHandle(path=snap_a, sha="a" * 40, source=SOURCE_ATTESTED, tickets_path=None)
    spec = pf.PrefetchSpec(ticket_id=_TICKET_ID, graph=False)

    with _stub_ticket():
        # The REAL gate read-root context — activates use_code_root(snap_a) for attested.
        with gate_source.gate_read_root(handle):
            # Precondition: the gate really did pin A as the code root.
            assert current_code_root() == snap_a
            # gate_ops.completion_precheck passes the LIVE checkout as repo_root.
            section, _manifest = pf.assemble_prefetch(spec, repo_root=live_b)

    assert "PINNED_A_CONTENT" in section, (
        "prefetch must read the --ref-pinned snapshot (A) for verdict-bearing bodies; "
        f"section did not contain A's content:\n{section}"
    )
    assert "LIVE_B_CONTENT" not in section, (
        f"prefetch leaked the LIVE checkout (B) into the verifier's verdict evidence:\n{section}"
    )
    # _discover_test_globs must glob the sibling test from the pinned root (A). The test file
    # exists ONLY in A, so a glob rooted at the live checkout (B) would never find it: its
    # presence proves discovery re-rooted to A, and its body proves the read re-rooted to A.
    assert "PINNED_A_TEST" in section, (
        "glob-discovered test bodies must resolve to the pinned snapshot (A); the A-only "
        f"sibling test was not discovered/read from A:\n{section}"
    )


def test_prefetch_reads_live_checkout_when_no_gate_snapshot_active(tmp_path: Path) -> None:
    """Negative control: with NO attested snapshot active (local mode / no gate), the
    passed repo_root IS the correct read root, so behavior is preserved."""
    live_b, _snap_a = _fixture(tmp_path)
    spec = pf.PrefetchSpec(ticket_id=_TICKET_ID, graph=False)

    # No gate_read_root / use_code_root context: current_code_root() is None.
    assert current_code_root() is None
    with _stub_ticket():
        section, _manifest = pf.assemble_prefetch(spec, repo_root=live_b)

    assert "LIVE_B_CONTENT" in section, (
        "in local/no-gate mode the passed repo_root is the read root; body must be read"
    )
