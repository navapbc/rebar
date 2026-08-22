"""Sanctioned administrative-disposition close path (ticket fc20-2df1-426e-42ab).

THE PROBLEM. Store mining found the single largest FORCE_CLOSE class to be ADMINISTRATIVE
closes — duplicate, obsolete, superseded, wontfix — where completing the described work was
never the intent, so the completion verifier can never PASS them and operators reached for
``--force``. A narrow attested-disposition path existed but was doubly scoped down: to the
class set {duplicate, not_a_bug, escalated} and to ``bug`` tickets only, so an obsolete
task/story/epic had NO truthful exit. And the completion-FAIL message never mentioned the
path that exists (incident 9b70).

THE FIX (asserted here): the existing ``--class`` mechanism gains ``obsolete`` /
``superseded`` / ``wontfix``; a non-bug close may carry ``--class`` only from the new
``ADMINISTRATIVE_CLASSES`` subset (write-side, gate-independent); ``obsolete`` / ``wontfix``
REQUIRE a ``--reason`` which persists as a new present-only ``close_reason`` key on the close
STATUS event; reason-only administrative closes skip the billable verifier and mint a SIGNED
disposition attestation whose manifest carries the class+reason line; ``duplicate`` /
``superseded`` demand a live replacement link; and the completion-FAIL message finally
enumerates the applicable ``--class`` values so a wontfix-shaped ticket is offered a
truthful exit.
"""

from __future__ import annotations

import json
import subprocess
import typing
from pathlib import Path

import pytest

import rebar
from rebar import types as rebar_types
from rebar._commands import close_disposition, close_precheck, gates, transition, txn
from rebar._commands._seam import CommandError
from rebar.reducer import _processors_status, reduce_ticket

pytestmark = pytest.mark.unit

_NEW_CLASSES = ("obsolete", "superseded", "wontfix")
_SCHEMA_DIR = Path(rebar.__file__).parent / "schemas"


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
    """Enable ONLY the completion-verification close gate; every other gate stays off."""
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


# --- vocabulary drift-guards -------------------------------------------------------------


def test_administrative_classes_exact_membership_and_strict_subset():
    assert close_disposition.ADMINISTRATIVE_CLASSES == frozenset(
        {"duplicate", "obsolete", "superseded", "wontfix"}
    )
    assert close_disposition.ADMINISTRATIVE_CLASSES < close_disposition.DISPOSITION_CLASSES


@pytest.mark.parametrize("new_class", _NEW_CLASSES)
def test_new_classes_reach_every_single_sourced_consumer(new_class):
    assert new_class in txn.CLOSE_CLASSES
    assert new_class in close_disposition.DISPOSITION_CLASSES
    assert new_class in close_precheck._NON_COMPLETION_BUG_CLASSES
    assert new_class in typing.get_args(rebar_types.CloseClass)


def test_close_classes_tuple_matches_schema_enum_order():
    schema = json.loads((_SCHEMA_DIR / "common.schema.json").read_text())
    enum = schema["$defs"]["close_class"]["enum"]
    assert tuple(enum) == txn.CLOSE_CLASSES


def test_ticket_state_schema_declares_close_reason():
    schema = json.loads((_SCHEMA_DIR / "ticket_state.schema.json").read_text())
    assert "close_reason" in schema["properties"]


# --- write-side enforcement (gate-independent: the gate is OFF in this store) -------------


def test_task_closes_obsolete_with_reason_and_persists_close_reason(store):
    tid = _make(store, "task", "premise gone")
    rc = _close(store, tid, "--class=obsolete", "--reason=premise no longer holds")

    assert rc == 0
    state = _state(store, tid)
    assert state["status"] == "closed"
    assert state["close_class"] == "obsolete"
    assert state["close_reason"] == "premise no longer holds"
    assert "force_close_reason" not in state


def test_story_closes_wontfix_with_reason(store):
    tid = _make(store, "story", "deliberate decision")
    rc = _close(store, tid, "--class=wontfix", "--reason=won't pursue")

    assert rc == 0
    state = _state(store, tid)
    assert state["close_class"] == "wontfix"
    assert state["close_reason"] == "won't pursue"


def test_epic_closes_superseded_with_a_replacement_link(store):
    epic = _make(store, "epic", "old plan")
    replacement = _make(store, "epic", "new plan", claim=False)
    rebar.link(replacement, epic, "supersedes", repo_root=str(store))

    rc = _close(store, epic, "--class=superseded")

    assert rc == 0
    state = _state(store, epic)
    assert state["close_class"] == "superseded"
    assert "close_reason" not in state


def test_task_closes_duplicate_with_an_outbound_duplicates_link(store):
    canonical = _make(store, "task", "canonical", claim=False)
    tid = _make(store, "task", "the copy")
    rebar.link(tid, canonical, "duplicates", repo_root=str(store))

    rc = _close(store, tid, "--class=duplicate")

    assert rc == 0
    assert _state(store, tid)["close_class"] == "duplicate"


@pytest.mark.parametrize("bug_only_class", ["not_a_bug", "escalated", "regression", "flaky"])
def test_non_bug_close_refuses_non_administrative_classes(store, capsys, bug_only_class):
    tid = _make(store, "task", "not administrative")

    rc = _close(store, tid, f"--class={bug_only_class}")

    assert rc == 1
    err = capsys.readouterr().err
    for allowed in ("duplicate", "obsolete", "superseded", "wontfix"):
        assert allowed in err, f"refusal must name the allowed values, got: {err}"
    assert _state(store, tid)["status"] == "in_progress"


@pytest.mark.parametrize("reason_class", ["obsolete", "wontfix"])
def test_reason_required_classes_refuse_a_close_without_a_reason(store, capsys, reason_class):
    tid = _make(store, "task", "needs a reason")

    rc = _close(store, tid, f"--class={reason_class}")

    assert rc == 1
    err = capsys.readouterr().err
    assert "--reason" in err
    assert _state(store, tid)["status"] == "in_progress"


def test_reason_requirement_is_enforced_below_the_cli(store):
    """The refusal lives in txn.transition_core (write-side), not just CLI flag parsing."""
    tid = _make(store, "task", "library-level close")
    tracker = str(store / ".tickets-tracker")

    with pytest.raises(CommandError, match="--reason"):
        txn.transition_core(
            tracker,
            tid,
            "in_progress",
            "closed",
            env_id="e",
            author="a",
            close_class="wontfix",
            repo_root=str(store),
        )


def test_non_bug_class_vocabulary_is_enforced_below_the_cli(store):
    tid = _make(store, "task", "library-level class check")
    tracker = str(store / ".tickets-tracker")

    with pytest.raises(CommandError, match="not_a_bug"):
        txn.transition_core(
            tracker,
            tid,
            "in_progress",
            "closed",
            env_id="e",
            author="a",
            close_class="not_a_bug",
            repo_root=str(store),
        )


def test_administrative_vocabulary_refusal_applies_even_to_a_force_close(store, capsys):
    """--force bypasses gates, never the write-side class vocabulary."""
    tid = _make(store, "task", "forced with a bug-only class")

    rc = _close(store, tid, "--class=escalated", "--force=operator override")

    assert rc == 1
    assert _state(store, tid)["status"] == "in_progress"


@pytest.mark.parametrize(
    "close_class",
    sorted(close_disposition.REASON_REQUIRED_CLASSES & close_disposition.ADMINISTRATIVE_CLASSES),
)
def test_forced_administrative_close_accepts_force_reason_as_justification(store, close_class):
    """--force=<reason> satisfies the reason requirement (the force_close_reason arm of
    txn.close_class_refusal): the bypass note IS the justification. It persists as
    force_close_reason; close_reason stays absent because no non-force --reason was given."""
    tid = _make(store, "task", f"forced {close_class}")

    rc = _close(store, tid, f"--class={close_class}", "--force=operator: premise gone")

    assert rc == 0
    state = _state(store, tid)
    assert state["status"] == "closed"
    assert state["close_class"] == close_class
    assert state["force_close_reason"] == "operator: premise gone"
    assert "close_reason" not in state


def test_library_close_reason_is_gated_on_the_admitting_class(store, monkeypatch):
    """rebar.transition mirrors the CLI's admission rule: a reason alongside a class that
    does not take one is discarded, never persisted — the library and CLI cannot drift.
    Asserted at the transition_compute boundary too, so the library's own gating is pinned
    independently of transition_core's write-side backstop."""
    bug = _make(store, "bug", "library smuggling probe")
    seen: dict = {}
    real_compute = transition.transition_compute

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(transition, "transition_compute", spy)

    rebar.transition(
        bug,
        "in_progress",
        "closed",
        reason="smuggled rationale",
        close_class="regression",
        repo_root=str(store),
    )

    assert seen["close_reason"] == ""
    state = _state(store, bug)
    assert state["close_class"] == "regression"
    assert "close_reason" not in state


def test_library_close_persists_the_reason_for_a_reason_required_class(store):
    tid = _make(store, "task", "library obsolete close")

    rebar.transition(
        tid,
        "in_progress",
        "closed",
        reason="premise no longer holds",
        close_class="obsolete",
        repo_root=str(store),
    )

    state = _state(store, tid)
    assert state["close_class"] == "obsolete"
    assert state["close_reason"] == "premise no longer holds"


def test_bug_close_still_requires_a_class(store, capsys):
    bug = _make(store, "bug", "unchanged bug behavior")

    rc = _close(store, bug)

    assert rc == 1
    assert "closing a bug ticket requires --class" in capsys.readouterr().err


def test_bug_close_with_an_existing_class_is_unchanged(store):
    bug = _make(store, "bug", "regression close")

    rc = _close(store, bug, "--class=regression")

    assert rc == 0
    state = _state(store, bug)
    assert state["close_class"] == "regression"
    assert "close_reason" not in state


def test_bug_can_use_the_new_reason_required_classes_too(store):
    bug = _make(store, "bug", "wontfix bug")

    rc = _close(store, bug, "--class=wontfix", "--reason=deliberate")

    assert rc == 0
    state = _state(store, bug)
    assert state["close_class"] == "wontfix"
    assert state["close_reason"] == "deliberate"


# --- the --reason flag guard (transition_cli) ---------------------------------------------


def test_plain_reason_on_a_non_close_transition_is_still_refused(store, capsys):
    tid = rebar.create_ticket("task", "no reason on claim", repo_root=str(store))

    rc = transition.transition_cli([tid, "open", "in_progress", "--reason=nope"])

    assert rc == 1
    assert "--force" in capsys.readouterr().err


def test_plain_reason_on_a_close_without_a_reason_capable_class_is_refused(store, capsys):
    canonical = _make(store, "task", "canonical", claim=False)
    tid = _make(store, "task", "duplicate with stray reason")
    rebar.link(tid, canonical, "duplicates", repo_root=str(store))

    rc = _close(store, tid, "--class=duplicate", "--reason=stray")

    assert rc == 1
    assert "--force" in capsys.readouterr().err
    assert _state(store, tid)["status"] == "in_progress"


# --- reducer fold --------------------------------------------------------------------------


def test_fold_close_metadata_folds_close_reason_on_the_closed_edge():
    state: dict = {}
    _processors_status._fold_close_metadata(
        state, {"status": "closed", "close_class": "obsolete", "close_reason": "why"}
    )
    assert state["close_reason"] == "why"
    assert state["close_class"] == "obsolete"


def test_fold_close_metadata_leaves_close_reason_absent_when_not_stamped():
    state: dict = {}
    _processors_status._fold_close_metadata(state, {"status": "closed"})
    assert "close_reason" not in state


def test_fold_close_metadata_ignores_close_reason_on_a_non_close_edge():
    state: dict = {}
    _processors_status._fold_close_metadata(state, {"status": "blocked", "close_reason": "x"})
    assert "close_reason" not in state


# --- attestation (verdict + manifest) ------------------------------------------------------


def test_verdict_mints_a_reason_only_disposition_without_a_replacement():
    result = close_disposition.verdict(
        "tkt-0001", "obsolete", "/nonexistent/tracker", close_reason="premise gone"
    )

    assert result is not None
    assert result["verdict"] == "PASS"
    assert result["disposition"] == "obsolete"
    assert result["close_reason"] == "premise gone"


def test_verdict_refuses_a_reason_only_class_without_a_reason():
    assert close_disposition.verdict("tkt-0001", "wontfix", "/nonexistent/tracker") is None


def test_decorate_manifest_signs_the_class_and_reason_line():
    out = close_disposition.decorate_manifest(
        ["completion-verifier: PASS", "ticket: tkt-0001", "material: cafebabe"],
        {"disposition": "wontfix", "close_reason": "deliberate decision"},
    )

    assert out[0] == "completion-verifier: DISPOSITION wontfix"
    assert out[1] == "disposition: wontfix reason: deliberate decision"
    assert "material: cafebabe" in out


def test_decorate_manifest_keeps_the_replacement_line_for_replacement_bearing_classes():
    out = close_disposition.decorate_manifest(
        ["completion-verifier: PASS", "ticket: tkt-0001"],
        {"disposition": "superseded", "replacement": "dead-beef-cafe-0001"},
    )

    assert out[0] == "completion-verifier: DISPOSITION superseded"
    assert out[1] == "replacement: dead-beef-cafe-0001"


# --- the precheck branch (completion gate ON) ----------------------------------------------


def test_reason_only_close_skips_the_verifier_and_returns_the_disposition(store, monkeypatch):
    _arm_completion_gate(monkeypatch)
    _forbid_verifier(monkeypatch)
    tid = _make(store, "task", "obsolete under the gate")

    out, expectation = close_precheck._completion_precheck(
        tid,
        "task",
        str(store),
        str(store),
        reason="premise gone",
        force_close="",
        close_class="obsolete",
    )

    assert out is not None, "an unsigned close for a valid disposition is the 738a trap"
    assert expectation == "disposition"
    assert out["disposition"] == "obsolete"
    assert out["close_reason"] == "premise gone"


@pytest.mark.parametrize(
    ("ticket_type", "close_class", "flags"),
    [
        ("task", "obsolete", ("--class=obsolete", "--reason=premise gone")),
        ("story", "wontfix", ("--class=wontfix", "--reason=deliberate")),
        ("epic", "obsolete", ("--class=obsolete", "--reason=direction changed")),
    ],
)
def test_non_bug_tickets_close_administratively_without_completion_verification(
    store, monkeypatch, ticket_type, close_class, flags
):
    _arm_completion_gate(monkeypatch)
    _forbid_verifier(monkeypatch)
    tid = _make(store, ticket_type, f"{ticket_type} closes {close_class}")

    rc = _close(store, tid, *flags)

    assert rc == 0
    state = _state(store, tid)
    assert state["status"] == "closed"
    assert state["close_class"] == close_class


def test_a_reason_only_disposition_close_is_signed_with_the_reason_line(store, monkeypatch):
    """AC: an unsigned close for a valid disposition is RED."""
    _arm_completion_gate(monkeypatch)
    _forbid_verifier(monkeypatch)
    tid = _make(store, "task", "signed disposition")

    rc = _close(store, tid, "--class=obsolete", "--reason=premise gone")

    assert rc == 0
    attestations = _state(store, tid).get("attestations") or {}
    record = attestations.get("completion-verifier")
    assert record, "the disposition close must mint a completion-verifier attestation"
    manifest = record.get("manifest") or []
    assert manifest[0] == "completion-verifier: DISPOSITION obsolete"
    assert "disposition: obsolete reason: premise gone" in manifest


def test_a_replacement_bearing_disposition_close_signs_the_replacement_line(store, monkeypatch):
    _arm_completion_gate(monkeypatch)
    _forbid_verifier(monkeypatch)
    tid = _make(store, "story", "superseded story")
    replacement = _make(store, "story", "the replacement", claim=False)
    rebar.link(replacement, tid, "supersedes", repo_root=str(store))

    rc = _close(store, tid, "--class=superseded")

    assert rc == 0
    attestations = _state(store, tid).get("attestations") or {}
    record = attestations.get("completion-verifier")
    assert record, "a replacement-bearing disposition close must still attest"
    manifest = record.get("manifest") or []
    assert manifest[0] == "completion-verifier: DISPOSITION superseded"
    assert f"replacement: {replacement}" in manifest


@pytest.mark.parametrize(
    ("link_class", "relation_word"),
    [("superseded", "supersedes"), ("duplicate", "duplicates")],
)
def test_replacement_bearing_classes_demand_a_live_link_on_non_bugs(
    store, monkeypatch, capsys, link_class, relation_word
):
    _arm_completion_gate(monkeypatch)
    _forbid_verifier(monkeypatch)
    tid = _make(store, "task", f"{link_class} without a link")

    rc = _close(store, tid, f"--class={link_class}")

    assert rc == 1
    assert relation_word in capsys.readouterr().err
    assert _state(store, tid)["status"] == "in_progress"


# --- discoverability -----------------------------------------------------------------------


def test_completion_fail_message_offers_the_truthful_administrative_exit(store, monkeypatch):
    """The 9b70 oracle: a wontfix-shaped ticket's failure message must present a real exit."""
    _arm_completion_gate(monkeypatch)
    import rebar.llm as _llm

    monkeypatch.setattr(
        _llm,
        "verify_completion",
        lambda *_a, **_k: {
            "verdict": "FAIL",
            "findings": [{"criterion": "work", "detail": "not done"}],
        },
    )
    tid = _make(store, "task", "wontfix-shaped work")

    with pytest.raises(CommandError) as caught:
        close_precheck._completion_precheck(
            tid, "task", str(store), str(store), reason="", force_close="", close_class=""
        )

    message = str(caught.value)
    assert "--class" in message
    for value in ("wontfix", "obsolete", "superseded", "duplicate"):
        assert value in message, f"the FAIL message must enumerate {value}; got: {message}"
    assert "not_a_bug" not in message, "bug-only classes are not a truthful exit for a task"


def test_completion_fail_message_enumerates_the_bug_vocabulary_for_bugs(store, monkeypatch):
    _arm_completion_gate(monkeypatch)
    import rebar.llm as _llm

    monkeypatch.setattr(
        _llm,
        "verify_completion",
        lambda *_a, **_k: {
            "verdict": "FAIL",
            "findings": [{"criterion": "work", "detail": "not done"}],
        },
    )
    bug = _make(store, "bug", "failing bug close")

    with pytest.raises(CommandError) as caught:
        close_precheck._completion_precheck(
            bug, "bug", str(store), str(store), reason="", force_close="", close_class="flaky"
        )

    message = str(caught.value)
    assert "not_a_bug" in message


def test_force_close_hints_the_disposition_alternative(store, capsys):
    tid = _make(store, "task", "forced administrative-shaped close")

    rc = _close(store, tid, "--force=just close it")

    assert rc == 0
    err = capsys.readouterr().err
    assert "--class" in err, f"a force close must hint the disposition alternative; got: {err}"
