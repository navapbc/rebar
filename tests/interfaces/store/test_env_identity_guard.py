"""A tracker re-clone must not silently re-identify the environment (bug
gold-distinct-lacewing).

``.env-id`` is git-ignored, is stamped into every event, and is the ``principal`` of every
op-cert attestation. Re-cloning the tracker without carrying it over used to mint a fresh
uuid with no signal at all, and every attestation signed by the old identity quietly
became unverifiable — surfacing hours later as a per-ticket ``foreign_key`` verdict at a
claim gate, when the real condition is store-wide.

These tests pin the behaviours that make that impossible to miss: minting into a store
that already holds another environment's events warns loudly, an explicit override
acknowledges it, and ``fsck`` reports the divergence store-wide however it arose.

They also pin the two false-positive boundaries the first cut got wrong. A second clone
mounting a shared tickets branch is a FIRST-CLASS workflow, so the mint must not be
refused; and such a store legitimately holds several env ids, so the detector reports only
ONE env id signed by two keys at once — never several env ids, and never one author under
several env ids, which is how two services sharing a bot account look.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

import rebar
from rebar._commands import fsck as _fsck
from rebar._commands.fsck_authorship import EnvAuthorshipTally
from rebar._store import env_identity
from rebar.attest import dsse

_OTHER_ENV = "ade0f2ef-51cc-45a2-bb97-c09846cd3df5"


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _restamp_events(tracker: Path, env_id: str, *, author: str | None = None) -> int:
    """Rewrite every event in the store as if another environment had written it."""
    count = 0
    for ticket_dir in tracker.iterdir():
        if ticket_dir.name.startswith(".") or not ticket_dir.is_dir():
            continue
        for event in ticket_dir.glob("*.json"):
            payload = json.loads(event.read_text(encoding="utf-8"))
            payload["env_id"] = env_id
            if author is not None:
                payload["author"] = author
            event.write_text(json.dumps(payload), encoding="utf-8")
            count += 1
    return count


def _foreign_store(repo: Path) -> Path:
    """A store holding events from ``_OTHER_ENV`` and NO ``.env-id`` — a re-clone."""
    rebar.create_ticket("task", "written elsewhere", repo_root=str(repo))
    tracker = _tracker(repo)
    assert _restamp_events(tracker, _OTHER_ENV) > 0, "fixture wrote no events"
    (tracker / env_identity.ENV_ID_FILE).unlink()
    return tracker


# ── minting ──────────────────────────────────────────────────────────────────


def test_genesis_mints_silently(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A store with no events is a NEW environment — mint, say nothing."""
    tracker = _tracker(rebar_repo)
    (tracker / env_identity.ENV_ID_FILE).unlink()
    for ticket_dir in list(tracker.iterdir()):
        if not ticket_dir.name.startswith(".") and ticket_dir.is_dir():
            for event in ticket_dir.glob("*.json"):
                event.unlink()

    capsys.readouterr()
    outcome = env_identity.ensure_env_id_unit(str(tracker))

    assert outcome.status == "changed"
    assert (tracker / env_identity.ENV_ID_FILE).is_file()
    assert "WARNING" not in capsys.readouterr().err


def test_converged_store_is_a_noop(rebar_repo: Path) -> None:
    tracker = _tracker(rebar_repo)
    before = (tracker / env_identity.ENV_ID_FILE).read_text(encoding="utf-8")

    outcome = env_identity.ensure_env_id_unit(str(tracker))

    assert outcome.status == "ok"
    assert (tracker / env_identity.ENV_ID_FILE).read_text(encoding="utf-8") == before


def test_reidentification_warns_loudly_and_names_the_prior_identity(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The held-out case: existing events + no ``.env-id`` is not silently accepted."""
    monkeypatch.delenv(env_identity.OVERRIDE_ENV, raising=False)
    tracker = _foreign_store(rebar_repo)
    capsys.readouterr()

    outcome = env_identity.ensure_env_id_unit(str(tracker))

    err = capsys.readouterr().err
    assert "WARNING" in err, "the re-identification was silent"
    assert _OTHER_ENV in err, "the warning does not name the prior environment"
    assert env_identity.OVERRIDE_ENV in err, "the warning does not name the override"
    assert "attestation" in err.lower(), "the warning does not name the consequence"
    assert ".opcert-key" in err, "the warning does not list the state to carry over"
    # It MINTS: refusing would break a second clone mounting a shared tickets branch.
    assert (tracker / env_identity.ENV_ID_FILE).is_file()
    assert outcome.status == "changed"
    assert _OTHER_ENV in outcome.detail, "the ensure outcome hides what it wrote over"


def _assert_states_the_current_attestation_policy(text: str) -> None:
    """A re-clone no longer invalidates op-certs (bug c21f-6f29-5d2d-4a5a): a cert from the
    lost identity verifies on the key embedded in its signature, and turns ``foreign_key``
    only under the opt-in ``verify.require_environment`` (bug westbound-chivalric-pinemarten)."""
    assert "verify.require_environment" in text, "the condition for foreign_key is not named"
    assert "foreign_key" in text
    # ADR 0125: validity-only by default, a per-environment trust list when one is set, and no
    # repository-pinned keys (bug childproof-adrenal-macaw).
    assert "verify.trusted_opcert_keys" in text, "the per-environment trust list is not named"
    assert "untrusted" in text, "the trust-list verdict is not named"
    assert "pinned" not in text, "still describes repository-pinned op-cert keys"
    for stale in ("unverifiable", "re-earned", "cannot be verified", "no existing attestation"):
        assert stale not in text, f"still claims attestations are lost: {stale!r}"


def test_reidentification_warning_says_old_attestations_still_certify() -> None:
    warning = env_identity.reidentification_warning({_OTHER_ENV}, acknowledged=False)

    _assert_states_the_current_attestation_policy(warning)
    assert ".opcert-key" in warning, "the carry-over remedy was dropped"


def test_mounting_a_shared_store_still_works(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second clone must end up writable, not blocked behind an override.

    RED against the first implementation, which refused the mint and broke
    ``test_two_clone_union_deterministic_replay_and_fork_tiebreak``."""
    monkeypatch.delenv(env_identity.OVERRIDE_ENV, raising=False)
    tracker = _foreign_store(rebar_repo)

    env_identity.ensure_env_id_unit(str(tracker))

    minted = env_identity.read_env_id(tracker)
    assert minted and minted != _OTHER_ENV
    rebar.create_ticket("task", "written by the new clone", repo_root=str(rebar_repo))


def test_override_acknowledges_and_quietens_the_warning(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_identity.OVERRIDE_ENV, "1")
    tracker = _foreign_store(rebar_repo)
    capsys.readouterr()

    outcome = env_identity.ensure_env_id_unit(str(tracker))

    assert outcome.status == "changed"
    minted = (tracker / env_identity.ENV_ID_FILE).read_text(encoding="utf-8").strip()
    assert minted and minted != _OTHER_ENV
    err = capsys.readouterr().err
    assert "WARNING" not in err, "an acknowledged re-identification still shouted"
    assert _OTHER_ENV in err, "the acknowledgement left no record of what was written over"


def test_override_env_name_matches_the_read_site(monkeypatch: pytest.MonkeyPatch) -> None:
    """``override_enabled`` reads a string LITERAL so the env-var registry can name it;
    this pins that literal to ``OVERRIDE_ENV`` so the two spellings cannot drift."""
    monkeypatch.setenv(env_identity.OVERRIDE_ENV, "1")
    assert env_identity.override_enabled() is True
    monkeypatch.setenv(env_identity.OVERRIDE_ENV, "0")
    assert env_identity.override_enabled() is False


def test_unreadable_events_do_not_crash_the_scan(rebar_repo: Path) -> None:
    tracker = _foreign_store(rebar_repo)
    garbage = next(d for d in tracker.iterdir() if d.is_dir() and not d.name.startswith("."))
    (garbage / "1700000000000000000-dead-CREATE.json").write_text("{not json", encoding="utf-8")

    assert env_identity.store_event_env_ids(tracker) == {_OTHER_ENV}


# ── the fsck detector ────────────────────────────────────────────────────────
#
# Environments are told apart by the authorship signing key embedded in each event's
# `author_sig`, not by author (operator decision on placid-dreary-zethuswasp, 2026-11-18):
# each environment signs with its own key, and checkouts in one environment may share it.
# One env id signed by 2 or more keys during OVERLAPPING periods means its `.env-id` was
# copied into another environment. Sequential keys are a rotation. The line is report-only.

_KEY_A = b"\x11" * 32
_KEY_B = b"\x22" * 32


def _ssh_string(raw: bytes) -> bytes:
    return len(raw).to_bytes(4, "big") + raw


def _author_sig(key: bytes) -> str:
    """A DSSE ``author_sig`` whose SSHSIG carries ``key`` as its embedded ed25519 key.

    Only the embedded public key matters to the detector, so the signature bytes are
    filler; building it by hand keeps the test free of ``ssh-keygen``."""
    pubkey = _ssh_string(b"ssh-ed25519") + _ssh_string(key)
    blob = b"SSHSIG" + (1).to_bytes(4, "big") + _ssh_string(pubkey) + _ssh_string(b"rebar")
    blob += _ssh_string(b"") + _ssh_string(b"sha512") + _ssh_string(b"\x00" * 16)
    armored = (
        "-----BEGIN SSH SIGNATURE-----\n"
        + base64.b64encode(blob).decode("ascii")
        + "\n-----END SSH SIGNATURE-----\n"
    ).encode("ascii")
    return dsse.encode(
        "application/vnd.rebar.authorship.v1+json", b"{}", [{"keyid": "someone", "sig": armored}]
    )


def _fingerprint(key: bytes) -> str:
    pubkey = _ssh_string(b"ssh-ed25519") + _ssh_string(key)
    digest = base64.b64encode(hashlib.sha256(pubkey).digest()).decode("ascii")
    return "SHA256:" + digest.rstrip("=")


def _event(env: str, key: bytes | None, ts: int, *, author: str = "Rebar Bot") -> dict:
    event: dict = {"env_id": env, "author": author, "timestamp": ts}
    if key is not None:
        event["author_sig"] = _author_sig(key)
    return event


def _report(*events: dict) -> str | None:
    tally = EnvAuthorshipTally()
    for index, event in enumerate(events):
        tally.observe(f"{index}-COMMENT.json", event)
    return env_identity.divergence_report(tally.key_spans())


def test_one_env_id_under_two_overlapping_keys_is_reported() -> None:
    line = _report(
        _event(_OTHER_ENV, _KEY_A, 100),
        _event(_OTHER_ENV, _KEY_A, 300),
        _event(_OTHER_ENV, _KEY_B, 200),
        _event(_OTHER_ENV, _KEY_B, 400),
    )
    assert line is not None and line.startswith("ENV_ID_MISMATCH:")
    assert _OTHER_ENV in line
    assert _fingerprint(_KEY_A) in line and _fingerprint(_KEY_B) in line
    assert ".env-id" in line, "the remedy does not name the file to remove"


def test_a_key_rotation_is_not_reported() -> None:
    assert (
        _report(
            _event(_OTHER_ENV, _KEY_A, 100),
            _event(_OTHER_ENV, _KEY_A, 200),
            _event(_OTHER_ENV, _KEY_B, 300),
            _event(_OTHER_ENV, _KEY_B, 400),
        )
        is None
    )


def test_a_shared_boundary_timestamp_counts_as_overlap() -> None:
    assert _report(_event(_OTHER_ENV, _KEY_A, 100), _event(_OTHER_ENV, _KEY_B, 100)) is not None


def test_checkouts_sharing_one_key_are_not_reported() -> None:
    """Several env ids under one key are checkouts of one environment."""
    here = "492a5c71-6cda-48a6-8134-02ff6a672739"
    assert (
        _report(
            _event(here, _KEY_A, 100),
            _event(_OTHER_ENV, _KEY_A, 150),
            _event(here, _KEY_A, 200),
        )
        is None
    )


def test_two_environments_with_their_own_keys_are_not_reported() -> None:
    """The server's case: the MCP server and the review bot both commit as "Rebar Bot",
    interleaved in time, each under its own env id and key."""
    review_bot = "ad49f17f-0000-4000-8000-000000000000"
    assert (
        _report(
            _event(_OTHER_ENV, _KEY_A, 100),
            _event(review_bot, _KEY_B, 150),
            _event(_OTHER_ENV, _KEY_A, 200),
            _event(review_bot, _KEY_B, 250),
        )
        is None
    )


def test_unsigned_and_unparseable_events_carry_no_key() -> None:
    garbled = _event(_OTHER_ENV, None, 150)
    garbled["author_sig"] = "not a dsse envelope"
    assert (
        _report(
            _event(_OTHER_ENV, _KEY_A, 100),
            _event(_OTHER_ENV, None, 150),
            garbled,
            _event(_OTHER_ENV, _KEY_A, 200),
        )
        is None
    )


def test_tally_records_each_keys_first_and_last_timestamp() -> None:
    tally = EnvAuthorshipTally()
    for name, event in (
        ("a-CREATE.json", _event("env-1", _KEY_A, 500)),
        ("b-COMMENT.json", _event("env-1", _KEY_A, 100)),
        ("c-COMMENT.json", _event("env-1", _KEY_B, 50)),
        ("d-COMMENT.json", _event("env-2", _KEY_A, 900)),
        ("e-COMMENT.json", {**_event("env-2", _KEY_B, 0), "timestamp": "soon"}),
        ("f-SNAPSHOT.json", _event("env-1", _KEY_B, 10_000)),
    ):
        tally.observe(name, event)

    assert tally.key_spans() == {
        ("env-1", _fingerprint(_KEY_A)): (100, 500),
        ("env-1", _fingerprint(_KEY_B)): (50, 50),
        ("env-2", _fingerprint(_KEY_A)): (900, 900),
    }


def _restamp_signed(tracker: Path, ticket_id: str, env_id: str, key: bytes, ts: int) -> None:
    for event in (tracker / ticket_id).glob("*.json"):
        payload = json.loads(event.read_text(encoding="utf-8"))
        payload.update(_event(env_id, key, ts))
        event.write_text(json.dumps(payload), encoding="utf-8")


def _store_with_a_copied_env_id(repo: Path) -> Path:
    tracker = _tracker(repo)
    for index, key in enumerate((_KEY_A, _KEY_B, _KEY_A)):
        ticket_id = rebar.create_ticket("task", f"written by key {index}", repo_root=str(repo))
        _restamp_signed(tracker, ticket_id, _OTHER_ENV, key, 100 + index)
    return tracker


def test_fsck_reports_a_copied_env_id_without_failing(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report-only: spans cover all history and events are never rewritten, so a counted
    overlap could never stop counting (operator decision, 2026-11-18)."""
    _store_with_a_copied_env_id(rebar_repo)

    rc = _fsck.fsck_cli([], repo_root=str(rebar_repo))
    out = capsys.readouterr().out

    assert "ENV_ID_MISMATCH" in out and _OTHER_ENV in out, out
    assert rc == 0, out


def test_fsck_json_marks_the_finding_uncounted(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _store_with_a_copied_env_id(rebar_repo)

    _fsck.fsck_cli(["--output", "json"], repo_root=str(rebar_repo))
    payload = json.loads(capsys.readouterr().out)

    found = [issue for issue in payload["issues"] if issue["kind"] == "env_id_mismatch"]
    assert found and found[0]["counted"] is False, payload["issues"]


def test_fsck_is_clean_for_a_native_store(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rebar.create_ticket("task", "native", repo_root=str(rebar_repo))

    _fsck.fsck_cli([], repo_root=str(rebar_repo))

    assert "ENV_ID_MISMATCH" not in capsys.readouterr().out
