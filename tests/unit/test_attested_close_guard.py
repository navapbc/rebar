"""Close-path attested-item guard (bug 2f56-313f-6175-41b1).

The completion verifier classifies ACs SOLELY from the author tag (ADR-0043) — by design it
never second-guesses ``[operator-attested]``. The rejection point for a LAUNDERED tag (a
code-verifiable criterion tagged to dodge repository verification) must therefore be
deterministic and pre-LLM. :func:`rebar._commands.txn.ensure_attested_items_valid` is that
guard: it blocks the close when a tagged AC item cites exact repo path/symbol evidence
(laundering) or lacks its ``provenance:`` continuation line (ADR-0043 x ADR-0016), and it
is wired into ``close_precheck._completion_precheck`` before the billable verifier call.
"""

from __future__ import annotations

import pytest

from rebar._commands import close_precheck, txn
from rebar._commands._seam import CommandError

pytestmark = pytest.mark.unit

_TID = "2f56-0000-0000-0001"

_PROVENANCE = (
    "      provenance: environment=production; principal=release-operator; "
    "privilege_posture=production-equivalent; instrument=live-call — console shows green"
)


def _install_state(monkeypatch, description: str | None) -> None:
    def _reduce(path):
        if description is None:
            raise FileNotFoundError(path)
        return {"status": "in_progress", "description": description}

    monkeypatch.setattr(txn, "reduce_ticket", _reduce)


def _ac(*items: str) -> str:
    return "## Acceptance Criteria\n" + "\n".join(items) + "\n"


# ── the guard itself ─────────────────────────────────────────────────────────────


def test_mistagged_code_verifiable_item_blocks_close(monkeypatch) -> None:
    """AC-2: a tagged item whose evidence is a repo path/symbol is REJECTED before close."""
    _install_state(
        monkeypatch,
        _ac("- [x] [operator-attested] scoping holds; proxy: tests/unit/test_scan_scoping.py"),
    )
    with pytest.raises(CommandError) as ei:
        txn.ensure_attested_items_valid(_TID, "/nonexistent-tracker")
    msg = str(ei.value)
    assert "tests/unit/test_scan_scoping.py" in msg
    assert "operator-attested" in msg.lower()
    # The remedy: untag and let the completion verifier check the repository.
    assert "tag" in msg.lower()
    assert ei.value.returncode == 1


def test_tagged_item_missing_provenance_blocks_close(monkeypatch) -> None:
    """AC-3: a tagged item with no ``provenance:`` continuation line is rejected."""
    _install_state(monkeypatch, _ac("- [ ] [operator-attested] the prod deploy is confirmed live"))
    with pytest.raises(CommandError) as ei:
        txn.ensure_attested_items_valid(_TID, "/nonexistent-tracker")
    msg = str(ei.value)
    assert "provenance" in msg.lower()
    assert "deploy is confirmed live" in msg


def test_tagged_item_with_incomplete_provenance_blocks_close(monkeypatch) -> None:
    _install_state(
        monkeypatch,
        _ac(
            "- [ ] [operator-attested] the prod deploy is confirmed live",
            "      provenance: environment=production — checked",
        ),
    )
    with pytest.raises(CommandError, match="provenance"):
        txn.ensure_attested_items_valid(_TID, "/nonexistent-tracker")


def test_legitimately_external_item_with_provenance_passes(monkeypatch) -> None:
    """AC-4: deploy/vote/console evidence WITH a complete provenance line still closes."""
    _install_state(
        monkeypatch,
        _ac("- [ ] [operator-attested] the prod deploy is confirmed live", _PROVENANCE),
    )
    assert txn.ensure_attested_items_valid(_TID, "/nonexistent-tracker") is None


def test_untagged_items_are_ignored(monkeypatch) -> None:
    _install_state(monkeypatch, _ac("- [x] shipped tests/unit/test_x.py", "- [ ] docs updated"))
    assert txn.ensure_attested_items_valid(_TID, "/nonexistent-tracker") is None


def test_laundering_is_reported_before_missing_provenance(monkeypatch) -> None:
    """A laundered item is the deeper defect: its remedy is UNTAG, not add-provenance —
    the message must not coach the author into decorating a mistagged item."""
    _install_state(
        monkeypatch,
        _ac("- [x] [operator-attested] holds; proxy: tests/unit/test_scan_scoping.py"),
    )
    with pytest.raises(CommandError) as ei:
        txn.ensure_attested_items_valid(_TID, "/nonexistent-tracker")
    assert "tests/unit/test_scan_scoping.py" in str(ei.value)


def test_unreadable_ticket_is_not_blocked_here(monkeypatch) -> None:
    _install_state(monkeypatch, None)  # reduce raises
    assert txn.ensure_attested_items_valid(_TID, "/nonexistent-tracker") is None


def test_non_dict_state_is_not_blocked_here(monkeypatch) -> None:
    monkeypatch.setattr(txn, "reduce_ticket", lambda path: ["not-a-dict"])
    assert txn.ensure_attested_items_valid(_TID, "/nonexistent-tracker") is None


# ── wiring: _completion_precheck runs the guard pre-LLM ──────────────────────────


def test_completion_precheck_invokes_the_guard(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    from rebar._commands import gates as _gates

    monkeypatch.setattr(_gates, "gate_enabled", lambda *a, **k: True)
    monkeypatch.setattr(close_precheck.config, "tracker_dir", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(txn, "ensure_ac_boxes_checked", lambda *a, **k: None)

    def _guard(ticket_id, tracker):
        calls.append(ticket_id)
        raise CommandError("Error: laundering", returncode=1)

    monkeypatch.setattr(txn, "ensure_attested_items_valid", _guard)

    import rebar.llm as _llm

    def _never(*a, **k):
        raise AssertionError("the billable verifier ran despite the deterministic block")

    monkeypatch.setattr(_llm, "verify_completion", _never, raising=False)

    with pytest.raises(CommandError, match="laundering"):
        close_precheck._completion_precheck(
            _TID, "task", str(tmp_path), None, reason="", force_close="", close_class=""
        )
    assert calls == [_TID]
