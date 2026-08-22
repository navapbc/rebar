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
        lambda *a, **k: (
            verdict if verdict is not None else {"verdict": "PASS", "findings": []},
            "required",
        ),
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

    for name in ("close_class", "force_reason", "completion_expectation"):
        assert name in signature.parameters, f"{name} should still be a parameter"
        assert body.count(name) > 1, (
            f"{name} is declared but never read in transition_core's body — the "
            "defiant-orthoclase-buck defect"
        )


# ── durable close provenance: completion_expectation (story mechanical-coherent-wolverine) ──
# "Closed with no attestation" used to collapse gate-off, --force, config-unreadable
# fail-open, local-source, certifiable-False withholding and signature-append failure into
# ONE indistinguishable state. The close STATUS event now records WHY a completion signature
# was or was not EXPECTED (never the outcome — the STATUS commits BEFORE signing is
# attempted), in the same locked write as the close, folded into reduced state.

_GATE_ON = "[verify]\nrequire_completion_verification_for_close = true\n"
# `[verify` never closes its table header -> tomllib raises -> ConfigError -> UNREADABLE.
_BROKEN_CONFIG = "[verify\nrequire_completion_verification_for_close = true\n"


def _write_config(repo: Path, text: str) -> None:
    from rebar import config

    (repo / "rebar.toml").write_text(text, encoding="utf-8")
    config.reset_config_cache()


def _pass_verifier(monkeypatch, extra: dict | None = None) -> None:
    """Stub ONLY the billable LLM call; every deterministic precheck still runs."""
    from rebar._commands import close_autoresume

    result = {"verdict": "PASS", "findings": []}
    result.update(extra or {})
    monkeypatch.setattr(close_autoresume, "verify_with_auto_resume", lambda *a, **k: dict(result))
    monkeypatch.setattr(transition_close, "_material_drifted", lambda *_a: False)


def _close_event(repo: Path, tid: str) -> dict:
    closes = [e for e in _status_events(repo, tid) if e["data"].get("status") == "closed"]
    assert len(closes) == 1
    return closes[0]


def _expectation(repo: Path, tid: str) -> str | None:
    return rebar.show_ticket(tid, repo_root=str(repo)).get("completion_expectation")


def test_expectation_rides_the_close_own_locked_write(repo, monkeypatch):
    """AC1: written in the SAME locked write as the close — the write-lock is acquired
    exactly ONCE for the whole close, so the provenance cannot be lost to a second
    acquisition timing out (the failure mode being recorded)."""
    from rebar._commands import txn

    tid = _open_ticket(repo)
    acquisitions: list[object] = []
    real = txn._acquire_write_lock

    def counting(tracker_dir):
        acquisitions.append(tracker_dir)
        return real(tracker_dir)

    monkeypatch.setattr(txn, "_acquire_write_lock", counting)

    rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))

    assert len(acquisitions) == 1, "the close must take exactly one write-lock"
    assert _close_event(repo, tid)["data"]["completion_expectation"] == "gate_off"


def test_reduced_state_separates_signed_lost_and_gate_off(repo, monkeypatch):
    """AC2: a later reader, holding no lock and reading only reduced state, tells apart
    closed-and-signed / closed-with-signature-LOST / closed-with-gate-off."""
    _write_config(repo, _GATE_ON)
    _pass_verifier(monkeypatch)

    t_signed = _open_ticket(repo)
    rebar.transition(t_signed, "in_progress", "closed", repo_root=str(repo))

    t_lost = _open_ticket(repo)

    def _boom(*_a, **_kw):
        raise _signing.SigningError("flock: could not acquire lock after 60s")

    monkeypatch.setattr(transition_close, "sign_completion_verdict", _boom)
    rebar.transition(t_lost, "in_progress", "closed", repo_root=str(repo))

    _write_config(repo, "")
    t_off = _open_ticket(repo)
    rebar.transition(t_off, "in_progress", "closed", repo_root=str(repo))

    readings = {}
    for name, tid in (("signed", t_signed), ("lost", t_lost), ("off", t_off)):
        state = rebar.show_ticket(tid, repo_root=str(repo))
        attested = bool((state.get("attestations") or {}).get("completion-verifier"))
        readings[name] = (state.get("completion_expectation"), attested)

    assert readings["signed"] == ("required", True)
    assert readings["lost"] == ("required", False)
    assert readings["off"] == ("gate_off", False)
    assert len(set(readings.values())) == 3, "all three states must read back distinctly"


def test_force_bypassed_is_recorded_as_itself(repo, monkeypatch):
    """AC3 + AC7: a --force past an ENABLED gate records force_bypassed, never gate_off —
    computed independently of verified_result (which is None for a forced close)."""
    _write_config(repo, _GATE_ON)
    tid = _open_ticket(repo)

    rebar.transition(tid, "in_progress", "closed", force="operator call", repo_root=str(repo))

    assert _expectation(repo, tid) == "force_bypassed"


def test_local_source_is_recorded_as_itself(repo, monkeypatch):
    """AC3: an opt-in local (unattested) verdict is its own state, never gate_off."""
    _write_config(repo, _GATE_ON)
    _pass_verifier(monkeypatch, {"source": "local"})
    tid = _open_ticket(repo)

    rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))

    assert _expectation(repo, tid) == "local_source"


def test_not_certifiable_is_recorded_as_itself(repo, monkeypatch):
    """AC3: certification withheld (uncertified descendant) is its own state, never gate_off."""
    _write_config(repo, _GATE_ON)
    _pass_verifier(monkeypatch, {"certifiable": False})
    tid = _open_ticket(repo)

    rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))

    assert _expectation(repo, tid) == "not_certifiable"


def test_unreadable_config_records_gate_unreadable_not_gate_off(repo, monkeypatch):
    """AC4: the fail-OPEN skip on an unreadable config is a FAULT and must not be laundered
    into the gate_off policy choice (consumes the GateState tri-state)."""
    tid = _open_ticket(repo)
    _write_config(repo, _BROKEN_CONFIG)

    rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))

    assert _expectation(repo, tid) == "gate_unreadable"


def test_disposition_close_records_disposition_not_required(repo, monkeypatch):
    """AC5: a qualifying administrative disposition (reason-required class with its
    --reason) is signed as a DISPOSITION, not a completion PASS, and reads back as such."""
    _write_config(repo, _GATE_ON)
    tid = rebar.create_ticket("bug", "a bug", repo_root=str(repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))

    rebar.transition(
        tid,
        "in_progress",
        "closed",
        close_class="obsolete",
        reason="the premise no longer holds",
        repo_root=str(repo),
    )

    assert _expectation(repo, tid) == "disposition"


def test_idea_to_closed_records_not_applicable(repo):
    """AC6: an idea -> closed is a REJECT/DROP, not a completion — the gate never applied."""
    _write_config(repo, _GATE_ON)
    tid = rebar.create_ticket("task", "an idea", repo_root=str(repo))
    rebar.transition(tid, "open", "idea", repo_root=str(repo))

    rebar.transition(tid, "idea", "closed", repo_root=str(repo))

    assert _expectation(repo, tid) == "not_applicable"


def test_force_close_under_a_disabled_gate_records_gate_off(repo):
    """AC7 (precedence): the gate state resolves FIRST — with the gate off there was no gate
    to bypass, so a forced close records gate_off while the reason stays durable in
    force_close_reason."""
    tid = _open_ticket(repo)

    rebar.transition(tid, "in_progress", "closed", force="belt and braces", repo_root=str(repo))

    state = rebar.show_ticket(tid, repo_root=str(repo))
    assert state["completion_expectation"] == "gate_off"
    assert state["force_close_reason"] == "belt and braces"


def test_a_legacy_close_event_without_the_field_reads_back_absent(tmp_path):
    """AC8: backward compatibility — a historical close STATUS event lacking the key reduces
    to state with the key ABSENT (unknown/legacy, never guessed)."""
    from rebar.reducer import reduce_ticket

    tdir = tmp_path / "aaaa-bbbb-cccc-dddd"
    tdir.mkdir()
    (tdir / "100-11111111-1111-1111-1111-111111111111-CREATE.json").write_text(
        json.dumps(
            {
                "timestamp": 100,
                "uuid": "11111111-1111-1111-1111-111111111111",
                "event_type": "CREATE",
                "env_id": "e",
                "author": "t",
                "data": {"ticket_type": "task", "title": "legacy"},
            }
        )
    )
    (tdir / "200-22222222-2222-2222-2222-222222222222-STATUS.json").write_text(
        json.dumps(
            {
                "timestamp": 200,
                "uuid": "22222222-2222-2222-2222-222222222222",
                "event_type": "STATUS",
                "env_id": "e",
                "author": "t",
                "data": {"status": "closed", "current_status": "open"},
            }
        )
    )

    state = reduce_ticket(str(tdir))

    assert state["status"] == "closed"
    assert "completion_expectation" not in state, (
        "an absent field on a legacy event must stay absent — unknown, never guessed"
    )
