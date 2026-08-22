"""A `--class duplicate` close must be TOLD it needs a replacement link (bug c8fd).

THE DEFECT. `_completion_precheck` exempts a duplicate/not_a_bug/escalated bug that names a live
replacement from completion verification. When no such replacement was found it fell THROUGH to the
ordinary completion verifier, which correctly failed — a duplicate's defect is not resolved by the
duplicate — but printed remediation offering only two impossible paths: finish work that belongs to
the canonical ticket, or mark the criterion `[operator-attested]`, which would be a false
attestation that the defect no longer reproduces. The one action that works, `rebar link <id>
<canonical> duplicates`, was never named. Observed on bug 9b70, where the close failed repeatedly
and was nearly escalated for a gate change before a single `rebar link 9b70 6a81 duplicates` made
it pass unchanged.

These tests pin the fix and the three properties that keep it safe: the gate asks only that the
link EXIST (a duplicate of an already-CLOSED canonical is the common case, so requiring an open
target would break the majority path); it costs no billable LLM request; and it does not touch
`not_a_bug` / `escalated` / ordinary close classes, none of which owe a `duplicates` link.
"""

from __future__ import annotations

import pytest

from rebar._commands import close_disposition, close_precheck
from rebar._commands._seam import CommandError

_BUG = "c8fd-1274-bcea-408e"
_CANONICAL = "6a81-0000-0000-0001"


class _Store:
    """A reduced-state store standing in for the tracker, keyed by ticket id."""

    def __init__(self, states: dict[str, dict], inbound: list[dict] | None = None) -> None:
        self.states = states
        self.inbound = inbound or []
        self.llm_calls: list[str] = []

    def reduce(self, path: str) -> dict:
        import os

        state = self.states.get(os.path.basename(str(path)))
        if state is None:
            raise FileNotFoundError(path)
        return state


def _install(monkeypatch, store: _Store, tmp_path) -> None:
    """Point every read the precheck performs at ``store``, and stub the billable tail.

    Patches the real module attributes the precheck imports lazily, so the production code path
    — including `_is_live_ticket`'s archived/deleted rules — runs unmodified.
    """
    import rebar.reducer as _reducer
    import rebar.reducer._inbound as _inbound
    from rebar._commands import gates as _gates
    from rebar._commands import txn as _txn
    from rebar._engine_support import descendants as _descendants
    from rebar._engine_support import field_reads as _field_reads
    from rebar._engine_support import resolver as _resolver

    monkeypatch.setattr(_gates, "gate_enabled", lambda *a, **k: True)
    monkeypatch.setattr(close_precheck.config, "tracker_dir", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(_reducer, "reduce_ticket", store.reduce)
    monkeypatch.setattr(
        _inbound,
        "find_inbound_relationships",
        lambda *a, **k: {"inbound_links": store.inbound},
    )
    monkeypatch.setattr(
        _resolver,
        "resolve_ticket_id",
        lambda tid, *a, **k: tid if tid in store.states else None,
    )
    # The completion tail: everything past the duplicate branch, stubbed so that reaching it is
    # observable (llm_calls grows) instead of exploding on unrelated I/O.
    monkeypatch.setattr(_txn, "ensure_ac_boxes_checked", lambda *a, **k: None)
    monkeypatch.setattr(_field_reads, "file_impact", lambda *a, **k: [])
    monkeypatch.setattr(_descendants, "list_descendants", lambda *a, **k: {})

    import rebar.llm as _llm
    from rebar.llm import completion_sidecar as _sidecar

    def _verify(ticket_id, *a, **k):
        store.llm_calls.append(str(ticket_id))
        return {"verdict": "PASS", "source": "local"}

    monkeypatch.setattr(_llm, "verify_completion", _verify, raising=False)
    monkeypatch.setattr(_sidecar, "emit", lambda *a, **k: True, raising=False)


def _bug_state(deps: list[dict] | None = None) -> dict:
    return {"status": "in_progress", "ticket_type": "bug", "deps": deps or []}


def _run(store, tmp_path, monkeypatch, *, close_class: str, force: str = ""):
    _install(monkeypatch, store, tmp_path)
    result, _expectation = close_precheck._completion_precheck(
        _BUG,
        "bug",
        str(tmp_path),
        None,
        reason="",
        force_close=force,
        close_class=close_class,
    )
    return result


def test_a_duplicate_close_with_no_link_names_the_exact_link_command(monkeypatch, tmp_path):
    """THE BUG. The failure must name the remedy, not leave the operator to guess it.

    Asserted on the literal command text because that is what was missing in the field: the old
    message was not wrong about the facts, it was wrong about what to DO, and a message that says
    'no replacement link' without showing the command reproduces the same dead end.
    """
    store = _Store({_BUG: _bug_state()})

    with pytest.raises(CommandError) as excinfo:
        _run(store, tmp_path, monkeypatch, close_class="duplicate")

    message = str(excinfo.value)
    assert f"rebar link {_BUG} <canonical> duplicates" in message
    assert _BUG in message
    assert excinfo.value.returncode == 1


def test_the_no_link_duplicate_close_never_spends_an_llm_request(monkeypatch, tmp_path):
    """The check is pure reduced-state reading, so it must fire BEFORE the billable verifier.

    Left after the LLM call the operator would still pay a request to be told the wrong thing,
    which is the exact cost this ticket exists to remove.
    """
    store = _Store({_BUG: _bug_state()})

    with pytest.raises(CommandError):
        _run(store, tmp_path, monkeypatch, close_class="duplicate")

    assert store.llm_calls == [], "the completion verifier ran on a close that cannot pass"


def test_a_duplicates_link_to_a_closed_canonical_still_takes_the_disposition_path(
    monkeypatch, tmp_path
):
    """THE OPERATOR'S CONFIRMED REFINEMENT, pinned as a regression.

    Closing a duplicate of an ALREADY-CLOSED canonical is the common case — the canonical is
    usually what got fixed. The gate must key on the link's EXISTENCE only. `_is_live_ticket`
    happens to permit a closed target today (it rejects only archived/deleted/unresolvable), so
    this test's job is to make that incidental property load-bearing: a future tightening that
    demanded an open target would break the majority path silently, and now breaks here instead.
    """
    store = _Store(
        {
            _BUG: _bug_state([{"relation": "duplicates", "target_id": _CANONICAL}]),
            _CANONICAL: {"status": "closed", "ticket_type": "bug", "deps": []},
        }
    )
    monkeypatch.setattr(
        close_disposition,
        "verdict",
        lambda tid, cc, tracker: {"verdict": "PASS", "disposition": cc},
    )

    out = _run(store, tmp_path, monkeypatch, close_class="duplicate")

    assert out == {"verdict": "PASS", "disposition": "duplicate"}
    assert store.llm_calls == []


def test_a_duplicates_link_to_an_open_canonical_still_takes_the_disposition_path(
    monkeypatch, tmp_path
):
    """The pre-existing exemption is untouched — the fix adds a failure arm, it does not narrow
    the passing one."""
    store = _Store(
        {
            _BUG: _bug_state([{"relation": "duplicates", "target_id": _CANONICAL}]),
            _CANONICAL: {"status": "open", "ticket_type": "bug", "deps": []},
        }
    )
    monkeypatch.setattr(
        close_disposition,
        "verdict",
        lambda tid, cc, tracker: {"verdict": "PASS", "disposition": cc},
    )

    assert _run(store, tmp_path, monkeypatch, close_class="duplicate") is not None


def test_a_dead_replacement_target_gets_a_different_remedy_than_naming_none(monkeypatch, tmp_path):
    """'The ticket you named is gone' and 'you named none' are different problems.

    Collapsing them would send an operator who already ran `rebar link` back to run it again with
    no hint that the target, not the link, is what broke.
    """
    archived = _Store(
        {
            _BUG: _bug_state([{"relation": "duplicates", "target_id": _CANONICAL}]),
            _CANONICAL: {"status": "archived", "ticket_type": "bug", "deps": []},
        }
    )
    with pytest.raises(CommandError) as dead_exc:
        _run(archived, tmp_path, monkeypatch, close_class="duplicate")

    missing = _Store({_BUG: _bug_state()})
    with pytest.raises(CommandError) as missing_exc:
        _run(missing, tmp_path, monkeypatch, close_class="duplicate")

    dead_message = str(dead_exc.value)
    assert _CANONICAL in dead_message, "the dead target must be named so it can be re-linked"
    assert dead_message != str(missing_exc.value)
    assert "records no 'duplicates' link" in str(missing_exc.value)
    assert "records no 'duplicates' link" not in dead_message


@pytest.mark.parametrize("close_class", ["plan_defect", "preexisting"])
def test_ordinary_close_classes_are_unaffected_by_the_missing_link(
    close_class, monkeypatch, tmp_path
):
    """Scope guard. Ordinary classes never owed a `duplicates` link. They must still reach
    the completion verifier exactly as before, or this fix would block closes it was never
    authorized to touch — including the close of this very ticket, which is a `plan_defect`.
    """
    store = _Store({_BUG: _bug_state()})

    assert _run(store, tmp_path, monkeypatch, close_class=close_class) is None
    assert store.llm_calls == [_BUG], (
        "the unaffected classes must still run completion verification"
    )


@pytest.mark.parametrize("close_class", ["not_a_bug", "escalated"])
def test_reason_required_dispositions_are_refused_pre_llm_not_routed_to_the_verifier(
    close_class, monkeypatch, tmp_path
):
    """`not_a_bug` asserts there IS no defect and `escalated` may point outside the tracker, so
    neither owes a `duplicates` link — but since bug d54b neither falls through to the completion
    verifier either (which would demand proof a nonexistent defect was fixed, an unpassable
    gate). With no replacement link and no `--reason`, the close is refused pre-LLM naming both
    doors, and never spends a billable request.
    """
    store = _Store({_BUG: _bug_state()})

    with pytest.raises(CommandError) as excinfo:
        _run(store, tmp_path, monkeypatch, close_class=close_class)

    message = str(excinfo.value)
    assert "--reason" in message
    assert "replacement" in message
    assert store.llm_calls == [], "the completion verifier ran on a close that cannot pass"


def test_a_force_close_still_bypasses_the_new_duplicate_check(monkeypatch, tmp_path):
    """`--force` is the operator's audited escape hatch and returns BEFORE this branch. If the new
    raise were placed above that early return, a genuine one-off would have no way out at all."""
    store = _Store({_BUG: _bug_state()})

    out = _run(store, tmp_path, monkeypatch, close_class="duplicate", force="operator call")
    assert out is None
