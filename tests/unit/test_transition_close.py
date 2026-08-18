"""Close landed-work precheck must not print unrelated ambiguity noise (bug af11).

The completion gate's deterministic referencing-commit precheck
(``rebar._commands.transition_close._referencing_commit_exists``) scans EVERY
reachable commit message and resolves every extracted candidate id through the
shared resolver. The resolver reports ambiguity to stderr — correct when the USER
supplied the ambiguous id, pure noise when the precheck is merely walking
unrelated historical commit references. Contract (ticket af11-ac5f-4e86-4d1d):

- the scan emits NO ambiguity diagnostics for unrelated historical refs;
- the gate DECISION is unchanged in both directions — a later valid full-ID
  trailer is still found (True), and absence of one still fails (False);
- explicit, user-supplied ambiguous ids (resolved outside the scan) keep their
  diagnostics.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing as _signing
from rebar._commands import transition_close
from rebar._commands.transition_close import _referencing_commit_exists
from rebar._ids import resolve_ticket_id

TARGET = "af99-1111-2222-3333"
AMBIG_PREFIX = "2f3c"


@pytest.fixture
def scan_env(tmp_path: Path) -> tuple[str, str]:
    """A tracker whose tickets make ``2f3c`` ambiguous, plus a real git repo whose
    history carries an ambiguous-prefix subject ref NEWER than the target's
    full-ID trailer (so the scan must resolve the ambiguous candidate first)."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    for name in (f"{AMBIG_PREFIX}-aaaa-0000-0001", f"{AMBIG_PREFIX}-bbbb-0000-0002", TARGET):
        (tracker / name).mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("commit", "--allow-empty", "-q", "-m", f"work\n\nrebar-ticket: {TARGET}")
    git("commit", "--allow-empty", "-q", "-m", f"{AMBIG_PREFIX}: unrelated historical work")
    return str(tracker), str(repo)


def test_scan_finds_target_without_ambiguity_noise(scan_env, capsys) -> None:
    """Preconditions hold: the prefix IS ambiguous in this tracker (control below),
    yet the scan neither prints the ambiguity error nor misses the full-ID commit."""
    tracker, repo = scan_env
    assert _referencing_commit_exists({TARGET}, tracker, repo) is True
    captured = capsys.readouterr()
    assert "Ambiguous" not in captured.err
    assert "Ambiguous" not in captured.out


def test_scan_still_fails_when_no_commit_references_the_ticket(scan_env, capsys) -> None:
    """Unchanged gate decision, failing direction: an id no commit references is
    still NOT found — quieting diagnostics must not loosen the precheck."""
    tracker, repo = scan_env
    assert _referencing_commit_exists({"dddd-9999-8888-7777"}, tracker, repo) is False
    captured = capsys.readouterr()
    assert "Ambiguous" not in captured.err
    assert "Ambiguous" not in captured.out


def test_explicit_ambiguous_prefix_keeps_its_diagnostic(scan_env, capsys) -> None:
    """Contrast control: the same resolver call a user-facing command makes (no
    quiet) still reports the ambiguity — proving the fixture's prefix is genuinely
    ambiguous AND that target-id diagnostics survive the fix."""
    tracker, _repo = scan_env
    assert resolve_ticket_id(AMBIG_PREFIX, tracker) is None
    assert "Ambiguous prefix" in capsys.readouterr().err


# ── completion-signature reporting (bug silvern-dewy-damselfly) ────────────────────────────
# The close COMMITS, releases the lock, and only THEN attempts to sign. A signing failure
# therefore leaves a ticket closed-WITHOUT-signature while the command still succeeds — and
# used to say so only via a stderr line whose text ("flock: could not acquire lock after 60s")
# described the CLOSE as having failed. These pin the machine-readable marker, its closed cause
# vocabulary, and the corrected message.


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=r, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    rebar.init_repo(repo_root=str(r))
    return r


def _open_ticket(repo: Path) -> str:
    tid = rebar.create_ticket("task", "a task", repo_root=str(repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    return tid


def _gate_on(monkeypatch, *, verdict=None):
    """Force the completion gate to have produced a verdict, so the signing tail runs."""
    monkeypatch.setattr(
        transition_close,
        "_completion_precheck",
        lambda *a, **k: verdict if verdict is not None else {"verdict": "PASS", "findings": []},
    )
    monkeypatch.setattr(transition_close, "_material_drifted", lambda *_a: False)


def test_a_failed_signature_reports_signed_false_with_the_cause(repo, monkeypatch):
    """AC1: the loss is machine-readable. Before this, the payload carried no field at all
    about the signature, so no caller could detect it."""
    _gate_on(monkeypatch)

    def _boom(*_a, **_kw):
        raise _signing.SigningError("flock: could not acquire lock after 60s")

    monkeypatch.setattr(transition_close, "sign_completion_verdict", _boom)
    tid = _open_ticket(repo)

    result = rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))

    assert result["completion_signature"]["signed"] is False
    assert result["completion_signature"]["cause"] == "sign_failed"
    assert "60s" in result["completion_signature"]["error"]
    assert rebar.show_ticket(tid, repo_root=str(repo))["status"] == "closed", (
        "the close LANDED — that is the whole point of reporting it separately"
    )


def test_a_signed_close_reports_signed_true(repo, monkeypatch):
    """AC2: the marker is present on success too, so a caller can branch on one field
    unconditionally rather than testing for its presence."""
    _gate_on(monkeypatch)
    monkeypatch.setattr(transition_close, "sign_completion_verdict", lambda *a, **k: {})
    tid = _open_ticket(repo)

    result = rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))

    assert result["completion_signature"] == {"signed": True, "cause": "signed", "error": ""}


def test_the_message_says_the_close_landed_not_that_it_failed(repo, monkeypatch, capsys):
    """AC3: the reported defect. An agent read the appended lock error and concluded its
    transition had failed, when the close had committed ~60s earlier."""
    _gate_on(monkeypatch)

    def _boom(*_a, **_kw):
        raise _signing.SigningError("flock: could not acquire lock after 60s")

    monkeypatch.setattr(transition_close, "sign_completion_verdict", _boom)
    tid = _open_ticket(repo)

    rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))
    err = capsys.readouterr().err

    assert "IS CLOSED" in err and "the close committed" in err
    assert "Do NOT re-run the transition" in err


def test_material_drift_reports_its_own_cause(repo, monkeypatch):
    """AC5: drift also lands unsigned, but for a DIFFERENT reason — a deliberate refusal to
    attest stale state, not a failure — so it must not be reported as sign_failed."""
    _gate_on(monkeypatch)
    monkeypatch.setattr(transition_close, "_material_drifted", lambda *_a: True)
    tid = _open_ticket(repo)

    result = rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))

    assert result["completion_signature"]["signed"] is False
    assert result["completion_signature"]["cause"] == "material_drifted"


def test_a_plain_transition_carries_no_marker(repo):
    """The key is present only where a completion signature is in play, so its ABSENCE is
    meaningful: consumers read it as 'not a completion close'."""
    tid = rebar.create_ticket("task", "a task", repo_root=str(repo))

    result = rebar.transition(tid, "open", "in_progress", repo_root=str(repo))

    assert "completion_signature" not in result


def test_idea_to_closed_carries_no_marker(repo):
    """`idea -> closed` is a REJECT/DROP of an undesigned idea, not a completion: it is
    excluded from the signing block entirely, so inventing a cause for it would misreport a
    deliberate non-event as a signing result."""
    tid = rebar.create_ticket("task", "an idea", repo_root=str(repo))
    rebar.transition(tid, "open", "idea", repo_root=str(repo))

    result = rebar.transition(tid, "idea", "closed", repo_root=str(repo))

    assert "completion_signature" not in result


def test_the_cli_json_output_forwards_the_marker(repo, monkeypatch, capsys):
    """AC4, CLI surface. The CLI rebuilds the payload field by field, so without explicit
    forwarding the marker never reaches a consumer parsing --output json."""
    from rebar._cli import main

    _gate_on(monkeypatch)

    def _boom(*_a, **_kw):
        raise _signing.SigningError("signing unavailable")

    monkeypatch.setattr(transition_close, "sign_completion_verdict", _boom)
    tid = _open_ticket(repo)
    monkeypatch.chdir(repo)

    rc = main(["transition", tid, "in_progress", "closed", "-o", "json"])
    out = capsys.readouterr().out

    assert rc == 0, "exit code is deliberately unchanged — the marker carries the signal"
    payload = json.loads([ln for ln in out.splitlines() if ln.startswith("{")][-1])
    assert payload["completion_signature"]["cause"] == "sign_failed"


def test_a_force_close_reports_its_own_cause(repo, monkeypatch, capsys):
    """`--force` is a deliberate bypass, not a failure, but it still lands
    closed-without-signature — so it gets its own cause rather than being reported as though
    a signature had been attempted and lost."""
    from rebar._cli import main

    _gate_on(monkeypatch)
    tid = _open_ticket(repo)
    monkeypatch.chdir(repo)

    rc = main(["transition", tid, "in_progress", "closed", "--force=operator call", "-o", "json"])
    out = capsys.readouterr().out

    assert rc == 0
    payload = json.loads([ln for ln in out.splitlines() if ln.startswith("{")][-1])
    assert payload["completion_signature"] == {
        "signed": False,
        "cause": "force_bypassed",
        "error": "",
    }


# ── force-close reason durability (bug defiant-orthoclase-buck) ────────────────────────────
# `transition_core` accepted `force_close_reason` and never read it, so the operator's
# `--force=<reason>` was discarded before reaching the STATUS event. The only durable trace was
# a best-effort FORCE_CLOSE audit comment written AFTER the close via a SECOND lock acquisition
# and swallowed on failure — least reliable under exactly the contention that makes
# force-closing attractive. It now rides the close's own locked write.


def _status_events(repo: Path, ticket_id: str) -> list[dict]:
    """Every raw STATUS event for a ticket, oldest first, straight off disk.

    Resolved through rebar's own tracker resolver rather than a guessed path, so the
    assertion is about the EVENT's contents and cannot silently pass if the layout moves."""
    from rebar import config

    tdir = Path(config.tracker_dir(str(repo))) / ticket_id
    paths = sorted(tdir.glob("*-STATUS.json"))
    assert paths, f"no STATUS events found on disk for {ticket_id} under {tdir}"
    return [json.loads(p.read_text()) for p in paths]


def test_force_close_reason_is_written_onto_the_close_status_event(repo, monkeypatch):
    """AC1: the reason lands on the STATUS event itself — which IS the close's own locked
    write, so its durability costs no additional lock acquisition.

    The close no longer compacts (bug choosy-arthrodic-barbet), so the individual event
    survives to be read without disabling anything."""
    tid = _open_ticket(repo)
    monkeypatch.chdir(repo)
    from rebar._cli import main

    main(["transition", tid, "in_progress", "closed", "--force=gate is wedged, shipping"])

    closes = [e for e in _status_events(repo, tid) if e["data"].get("status") == "closed"]
    assert len(closes) == 1
    assert closes[0]["data"]["force_close_reason"] == "gate is wedged, shipping"


def test_the_reason_is_readable_from_reduced_state(repo, monkeypatch):
    """AC2: a later reader, holding no lock, sees WHY the gates were bypassed."""
    tid = _open_ticket(repo)
    monkeypatch.chdir(repo)
    from rebar._cli import main

    main(["transition", tid, "in_progress", "closed", "--force=operator judgement call"])

    shown = rebar.show_ticket(tid, repo_root=str(repo))
    assert shown["status"] == "closed"
    assert shown["force_close_reason"] == "operator judgement call"


def test_an_unforced_close_omits_the_key_entirely(repo, monkeypatch):
    """AC3: present-only. An ordinary close must stay byte-identical to the pre-change event
    shape, so absence of the key is itself the signal that the close was NOT forced."""
    tid = _open_ticket(repo)

    rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))

    closes = [e for e in _status_events(repo, tid) if e["data"].get("status") == "closed"]
    assert "force_close_reason" not in closes[0]["data"]
    assert "force_close_reason" not in rebar.show_ticket(tid, repo_root=str(repo))


def test_transition_core_does_not_accept_a_parameter_it_discards():
    """AC4: the defect itself was a parameter accepted and silently dropped. Pin that every
    close-metadata parameter `transition_core` declares is actually READ in its body, so a
    future one cannot rot into a no-op the way this one did."""
    import inspect

    from rebar._commands import txn

    source = inspect.getsource(txn.transition_core)
    signature = inspect.signature(txn.transition_core)
    body = source.split(":", 1)[1]  # drop the def line so params are not counted as reads

    for name in ("close_class", "force_reason"):
        assert name in signature.parameters, f"{name} should still be a parameter"
        assert body.count(name) > 1, (
            f"{name} is declared but never read in transition_core's body — the "
            "defiant-orthoclase-buck defect"
        )
