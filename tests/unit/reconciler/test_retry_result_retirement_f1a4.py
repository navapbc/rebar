"""Retirement guards for unreachable reconciler retry/result residue."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PASS_IO = REPO_ROOT / "src/rebar/reconciler/pass_io.py"
APPLIER = REPO_ROOT / "src/rebar/reconciler/applier.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_dead_retry_result_helpers_and_stale_owner_prose_are_gone() -> None:
    # applier.py now owns snapshot_head, the one survivor of the old _concurrency.py.
    applier = _text(APPLIER)
    assert "class ConcurrencyEvent" not in applier
    assert "class Result" not in applier
    assert "def rebase_retry" not in applier
    assert "When rebase_retry exhausts all write attempts" not in applier

    pass_io = _text(PASS_IO)
    assert "def _handle_failed_write_result" not in pass_io
    assert "when rebase_retry exhausts all write attempts" not in pass_io
