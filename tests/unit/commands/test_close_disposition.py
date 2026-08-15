"""A `not_a_bug`/`escalated` close without a replacement is reason-required (bug d54b).

THE DEFECT. `close_disposition.verdict()` minted a deterministic disposition verdict for
`not_a_bug`/`escalated` ONLY from a live replacement link. But `not_a_bug` inherently names no
replacement (it asserts there is no defect) and `escalated` may point outside the tracker, so
those closes fell through to FULL completion verification — which demands proof that a
nonexistent (or out-of-tracker) defect was fixed, an unpassable gate. Operators were forced
to `--force`, producing exactly the unsigned state the reopen ruling forbids.

THE FIX, reusing ticket fc20's reason-required machinery verbatim: both classes join
`REASON_REQUIRED_CLASSES`, with one refinement — `REPLACEMENT_SATISFIES_REASON_CLASSES`
({not_a_bug, escalated}) lets a live replacement link satisfy the requirement INSTEAD of
`--reason`, and the replacement is checked FIRST so the pre-d54b linked path is preserved
byte-for-byte. Neither present -> refused at write time naming both doors; the close never
reaches the completion verifier.

THE ESCALATED DECISION (advisory from plan-review, resolved here): `escalated` is
reason-required THE SAME WAY as `not_a_bug` — the structural problem is identical (no defect
fixed in-repo, verification unpassable), and replacement-preferred is preserved because a
live replacement link still short-circuits first. The `--reason` names where the work was
escalated to.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import close_disposition, gates, transition, txn
from rebar._commands._seam import CommandError
from rebar.reducer import reduce_ticket

pytestmark = pytest.mark.unit

_REASON_OR_LINK_CLASSES = ("escalated", "not_a_bug")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    monkeypatch.chdir(repo)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _make(repo: Path, ticket_type: str, title: str, *, claim: bool = True) -> str:
    tid = rebar.create_ticket(ticket_type, title, repo_root=str(repo))
    if claim:
        rebar.claim(tid, repo_root=str(repo))
    return tid


def _state(repo: Path, tid: str) -> dict:
    return reduce_ticket(str(repo / ".tickets-tracker" / tid)) or {}


def _close(repo: Path, tid: str, *flags: str) -> int:
    return transition.transition_cli([tid, "in_progress", "closed", *flags])


def _arm_completion_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gates,
        "gate_enabled",
        lambda _root, name, **_k: name == "require_completion_verification_for_close",
    )


def _forbid_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    import rebar.llm as _llm

    def _fail(*_a, **_k):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("the billable completion verifier must not run")

    monkeypatch.setattr(_llm, "verify_completion", _fail)


# --- vocabulary drift-guards ---------------------------------------------------------------


def test_reason_required_classes_exact_membership():
    assert close_disposition.REASON_REQUIRED_CLASSES == frozenset(
        {"obsolete", "wontfix", "not_a_bug", "escalated"}
    )


def test_replacement_satisfies_reason_classes_exact_membership_and_containment():
    """The replacement-exempt subset is exactly the bug-only pair, INSIDE the reason-required
    set and OUTSIDE the administrative (non-bug) vocabulary — a class in the wrong set would
    silently change which closes attest or which ticket types accept it."""
    assert close_disposition.REPLACEMENT_SATISFIES_REASON_CLASSES == frozenset(
        {"not_a_bug", "escalated"}
    )
    assert (
        close_disposition.REPLACEMENT_SATISFIES_REASON_CLASSES
        < close_disposition.REASON_REQUIRED_CLASSES
    )
    assert not (
        close_disposition.REPLACEMENT_SATISFIES_REASON_CLASSES
        & close_disposition.ADMINISTRATIVE_CLASSES
    )


# --- verdict precedence (unit, no store) ---------------------------------------------------


@pytest.mark.parametrize("close_class", _REASON_OR_LINK_CLASSES)
def test_verdict_mints_from_the_reason_when_no_replacement_exists(close_class):
    result = close_disposition.verdict(
        "tkt-0001", close_class, "/nonexistent/tracker", close_reason="RCA: no defect"
    )

    assert result is not None
    assert result["verdict"] == "PASS"
    assert result["disposition"] == close_class
    assert result["close_reason"] == "RCA: no defect"
    assert result["model"] == "none (deterministic disposition)"
    assert result["runner"] == "close_disposition"


@pytest.mark.parametrize("close_class", _REASON_OR_LINK_CLASSES)
def test_verdict_prefers_the_replacement_even_when_a_reason_is_given(close_class, monkeypatch):
    """PRECEDENCE: the replacement link wins; the reason mint is the fallback."""
    monkeypatch.setattr(
        close_disposition, "find_replacement", lambda *a, **k: "dead-beef-cafe-0001"
    )

    result = close_disposition.verdict(
        "tkt-0001", close_class, "/nonexistent/tracker", close_reason="also stated"
    )

    assert result is not None
    assert result["replacement"] == "dead-beef-cafe-0001"
    assert "close_reason" not in result, "the replacement mint is the pre-d54b shape, unchanged"


@pytest.mark.parametrize("close_class", _REASON_OR_LINK_CLASSES)
def test_verdict_still_declines_with_neither_reason_nor_replacement(close_class):
    """FAIL-CLOSED: an unjustified disposition must not sign (write-side refuses it anyway)."""
    assert close_disposition.verdict("tkt-0001", close_class, "/nonexistent/tracker") is None


@pytest.mark.parametrize("close_class", ["obsolete", "wontfix"])
def test_a_replacement_never_exempts_the_purely_reason_only_classes(close_class, monkeypatch):
    """obsolete/wontfix behavior is UNCHANGED: their justification IS the reason — a stray
    replacement link must neither substitute for it nor leak into the mint."""
    monkeypatch.setattr(
        close_disposition, "find_replacement", lambda *a, **k: "dead-beef-cafe-0001"
    )

    assert close_disposition.verdict("tkt-0001", close_class, "/nonexistent/tracker") is None
    result = close_disposition.verdict(
        "tkt-0001", close_class, "/nonexistent/tracker", close_reason="stated"
    )
    assert result is not None
    assert result["close_reason"] == "stated"
    assert "replacement" not in result


# --- write-side refusal (gate-independent: the gate is OFF in this store) ------------------


@pytest.mark.parametrize("close_class", _REASON_OR_LINK_CLASSES)
def test_close_without_reason_or_replacement_is_refused_naming_both_doors(
    store, capsys, close_class
):
    bug = _make(store, "bug", f"{close_class} with no justification")

    rc = _close(store, bug, f"--class={close_class}")

    assert rc == 1
    err = capsys.readouterr().err
    assert "--reason" in err, f"the refusal must name the flag to add; got: {err}"
    assert "replacement" in err, f"the refusal must name the link alternative; got: {err}"
    assert _state(store, bug)["status"] == "in_progress"


@pytest.mark.parametrize("close_class", _REASON_OR_LINK_CLASSES)
def test_refusal_is_enforced_below_the_cli(store, close_class):
    """The refusal lives in txn.transition_core (write-side), not just CLI flag parsing."""
    bug = _make(store, "bug", "library-level close")
    tracker = str(store / ".tickets-tracker")

    with pytest.raises(CommandError, match="--reason"):
        txn.transition_core(
            tracker,
            bug,
            "in_progress",
            "closed",
            env_id="e",
            author="a",
            close_class=close_class,
            repo_root=str(store),
        )


@pytest.mark.parametrize("close_class", _REASON_OR_LINK_CLASSES)
def test_a_live_replacement_link_satisfies_the_write_side_requirement(store, close_class):
    """transition_core consults the store: a linked close needs no --reason (pre-d54b path)."""
    canonical = _make(store, "task", "the canonical", claim=False)
    bug = _make(store, "bug", f"linked {close_class}")
    rebar.link(bug, canonical, "duplicates", repo_root=str(store))

    rc = _close(store, bug, f"--class={close_class}")

    assert rc == 0
    state = _state(store, bug)
    assert state["status"] == "closed"
    assert state["close_class"] == close_class
    assert "close_reason" not in state


# --- the close path under the completion gate (verifier must NEVER run) --------------------


@pytest.mark.parametrize("close_class", _REASON_OR_LINK_CLASSES)
def test_reasoned_close_skips_the_verifier_and_signs_the_reason_line(
    store, monkeypatch, close_class
):
    """AC: --class {not_a_bug,escalated} --reason=<text>, no --force, no replacement link ->
    closes without the billable verifier and mints a SIGNED disposition attestation carrying
    class+reason; close_reason persists on the close STATUS event."""
    _arm_completion_gate(monkeypatch)
    _forbid_verifier(monkeypatch)
    bug = _make(store, "bug", f"reasoned {close_class}")

    rc = _close(store, bug, f"--class={close_class}", "--reason=RCA: behaves as designed")

    assert rc == 0
    state = _state(store, bug)
    assert state["status"] == "closed"
    assert state["close_class"] == close_class
    assert state["close_reason"] == "RCA: behaves as designed"
    record = (state.get("attestations") or {}).get("completion-verifier")
    assert record, "the disposition close must mint a completion-verifier attestation"
    manifest = record.get("manifest") or []
    assert manifest[0] == f"completion-verifier: DISPOSITION {close_class}"
    assert f"disposition: {close_class} reason: RCA: behaves as designed" in manifest


@pytest.mark.parametrize("close_class", _REASON_OR_LINK_CLASSES)
def test_unjustified_close_is_refused_before_the_verifier_can_run(store, monkeypatch, close_class):
    """NOT fall-through: neither reason nor replacement -> actionable error, no LLM call."""
    _arm_completion_gate(monkeypatch)
    _forbid_verifier(monkeypatch)
    bug = _make(store, "bug", f"unjustified {close_class}")

    rc = _close(store, bug, f"--class={close_class}")

    assert rc == 1
    assert _state(store, bug)["status"] == "in_progress"


@pytest.mark.parametrize("close_class", _REASON_OR_LINK_CLASSES)
def test_replacement_link_still_short_circuits_without_a_reason(store, monkeypatch, close_class):
    _arm_completion_gate(monkeypatch)
    _forbid_verifier(monkeypatch)
    canonical = _make(store, "task", "canonical", claim=False)
    bug = _make(store, "bug", f"linked {close_class}")
    rebar.link(bug, canonical, "duplicates", repo_root=str(store))

    rc = _close(store, bug, f"--class={close_class}")

    assert rc == 0
    state = _state(store, bug)
    record = (state.get("attestations") or {}).get("completion-verifier")
    assert record, "a replacement-bearing disposition close must still attest"
    manifest = record.get("manifest") or []
    assert manifest[0] == f"completion-verifier: DISPOSITION {close_class}"
    assert f"replacement: {canonical}" in manifest


@pytest.mark.parametrize("close_class", _REASON_OR_LINK_CLASSES)
def test_replacement_link_wins_over_a_reason_in_the_signed_manifest(
    store, monkeypatch, close_class
):
    """PRECEDENCE end-to-end: linked AND reasoned -> the attestation names the replacement
    (pre-d54b shape); the reason still persists on the close event for the record."""
    _arm_completion_gate(monkeypatch)
    _forbid_verifier(monkeypatch)
    canonical = _make(store, "task", "canonical", claim=False)
    bug = _make(store, "bug", f"linked and reasoned {close_class}")
    rebar.link(bug, canonical, "duplicates", repo_root=str(store))

    rc = _close(store, bug, f"--class={close_class}", "--reason=also documented")

    assert rc == 0
    state = _state(store, bug)
    assert state["close_reason"] == "also documented"
    manifest = ((state.get("attestations") or {}).get("completion-verifier") or {}).get(
        "manifest"
    ) or []
    assert f"replacement: {canonical}" in manifest


# --- existing obsolete/wontfix behavior unchanged (regression pins) ------------------------


def test_obsolete_close_with_reason_still_works_end_to_end(store, monkeypatch):
    _arm_completion_gate(monkeypatch)
    _forbid_verifier(monkeypatch)
    tid = _make(store, "task", "obsolete unchanged")

    rc = _close(store, tid, "--class=obsolete", "--reason=premise gone")

    assert rc == 0
    state = _state(store, tid)
    assert state["close_class"] == "obsolete"
    assert state["close_reason"] == "premise gone"


def test_a_replacement_link_does_not_exempt_obsolete_from_its_reason(store, capsys):
    """The replacement door is scoped to not_a_bug/escalated ONLY."""
    replacement = _make(store, "task", "newer plan", claim=False)
    tid = _make(store, "task", "linked obsolete")
    rebar.link(tid, replacement, "duplicates", repo_root=str(store))

    rc = _close(store, tid, "--class=obsolete")

    assert rc == 1
    assert "--reason" in capsys.readouterr().err
    assert _state(store, tid)["status"] == "in_progress"


# --- the shared refusal rule (unit) --------------------------------------------------------


@pytest.mark.parametrize("close_class", _REASON_OR_LINK_CLASSES)
def test_close_class_refusal_names_both_doors_without_store_access(close_class):
    """Callers without a tracker (or with an unreadable one) still get the full refusal."""
    refusal = txn.close_class_refusal("bug", close_class)

    assert refusal is not None
    assert "--reason" in refusal
    assert "replacement" in refusal


@pytest.mark.parametrize("close_class", _REASON_OR_LINK_CLASSES)
def test_close_class_refusal_accepts_the_force_reason_as_justification(close_class):
    """--force=<reason> keeps satisfying the requirement, exactly as for obsolete/wontfix."""
    assert (
        txn.close_class_refusal("bug", close_class, force_close_reason="operator override") is None
    )
