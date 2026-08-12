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
refused; and such a store legitimately holds several env ids, so the detector is scoped
by AUTHOR — one author under two identities — rather than firing on every healthy
multi-clone store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rebar
from rebar._commands import fsck as _fsck
from rebar._store import env_identity

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


def _same_author_under_two_ids(repo: Path) -> tuple[Path, str, str]:
    """A store where THIS environment's author also wrote under ``_OTHER_ENV``."""
    rebar.create_ticket("task", "written before the re-clone", repo_root=str(repo))
    tracker = _tracker(repo)
    author = _sole_author(tracker)
    _restamp_events(tracker, _OTHER_ENV, author=author)
    rebar.create_ticket("task", "written after the re-clone", repo_root=str(repo))
    current = env_identity.read_env_id(tracker)
    assert current and current != _OTHER_ENV
    return tracker, current, author


def _sole_author(tracker: Path) -> str:
    authors = {author for _, author in _identity_pairs(tracker)}
    assert len(authors) == 1, f"fixture expected one author, got {authors}"
    return authors.pop()


def _identity_pairs(tracker: Path) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for ticket_dir in tracker.iterdir():
        if ticket_dir.name.startswith(".") or not ticket_dir.is_dir():
            continue
        for event in ticket_dir.glob("*.json"):
            env_identity.note_event_identity(json.loads(event.read_text(encoding="utf-8")), pairs)
    return pairs


def test_fsck_reports_env_id_divergence(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A completed re-identification is detected store-wide, not one gate at a time."""
    _tracker_path, current, _author = _same_author_under_two_ids(rebar_repo)

    rc = _fsck.fsck_cli([], repo_root=str(rebar_repo))
    out = capsys.readouterr().out

    assert "ENV_ID_MISMATCH" in out, "fsck did not report the environment-identity change"
    assert _OTHER_ENV in out and current in out, "the report does not name both ids"
    assert rc == 1, "the divergence was not counted as an integrity issue"


def test_fsck_json_carries_the_finding(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _same_author_under_two_ids(rebar_repo)

    _fsck.fsck_cli(["--output", "json"], repo_root=str(rebar_repo))
    payload = json.loads(capsys.readouterr().out)

    kinds = [issue["kind"] for issue in payload["issues"]]
    assert "env_id_mismatch" in kinds, f"not in --output json: {kinds}"


def test_a_shared_store_with_several_clones_is_not_reported(rebar_repo: Path) -> None:
    """The false-positive boundary: several env ids is the HEALTHY multi-clone topology.

    A detector that fired here would fire on every well-formed team store, and a line
    that is always present is a line nobody reads."""
    rebar.create_ticket("task", "mine", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)
    current = env_identity.read_env_id(tracker)
    peer = {(_OTHER_ENV, "a different agent")}

    assert env_identity.divergence_report(current, _identity_pairs(tracker) | peer) is None


def test_single_identity_store_is_clean(rebar_repo: Path) -> None:
    rebar.create_ticket("task", "native", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)

    assert (
        env_identity.divergence_report(env_identity.read_env_id(tracker), _identity_pairs(tracker))
        is None
    )


def test_divergence_report_is_silent_without_a_current_identity() -> None:
    """No ``.env-id`` yet is the mint warning's business, not the detector's — reporting
    "everything is foreign" there would be noise on a store that has not been identified."""
    assert env_identity.divergence_report("", {(_OTHER_ENV, "someone")}) is None
