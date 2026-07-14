"""Story 9e76: the reconciler's inbound writer signs its events inline.

`inbound_translate._write_event_file` must stamp attribution (`author_email` / `author_id`)
and, when a signing key is configured, sign (`author_sig`) each event it writes — reusing the
same seam helpers as `_seam.append_event` — so Jira-sourced reconciler events are attributable
and verifiable under `rebar verify-identity` instead of classifying as unknown-author/unsigned.

The write must stay best-effort + additive: no identity/key ⇒ the event is written UNSIGNED
(and older readers ignore the extra keys), never raising on the inbound path.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir
from rebar.attest import authorship, dsse, sshsig
from rebar.reducer import reduce_ticket
from rebar_reconciler import inbound_translate

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

_BOT_EMAIL = "bot@example.com"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", _BOT_EMAIL),  # resolves to the identity below
        ("git", "config", "user.name", "Bot"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _keypair(tmp_path: Path) -> tuple[str, str]:
    key = tmp_path / "botkey"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", "bot"],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / "botkey.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def _written_event(store: Path, ticket_id: str) -> tuple[dict, Path]:
    """Drive the inbound writer for one COMMENT event on a real ticket; return the parsed
    on-disk event JSON and the ticket dir (so callers can verify the sig / reduce the ticket)."""
    # The seam caches attribution per repo_root for the process lifetime; creating the
    # identity mid-test poisoned it with a pre-identity (no author_id) result. A real
    # reconciler process starts with the identity already existing, so clear the cache here
    # to simulate that fresh-process resolution.
    from rebar._commands import _seam

    _seam._ATTRIBUTION_CACHE.clear()
    tracker = Path(tracker_dir(str(store)))
    inbound_translate._write_event_file(tracker, ticket_id, "COMMENT", {"body": "from jira"})
    ticket_dir = tracker / ticket_id
    files = sorted(ticket_dir.glob("*-COMMENT.json"))
    assert files, "inbound writer produced no COMMENT event file"
    return json.loads(files[-1].read_text(encoding="utf-8")), ticket_dir


def _make_ticket(store: Path) -> str:
    """Create a real ticket (a genesis CREATE event) so the reducer can fold events on it."""
    return str(rebar.create_ticket("task", "inbound target", repo_root=str(store)))


def test_inbound_write_stamps_and_signs_as_identity(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a resolvable identity + signing key, the inbound writer stamps author_id/
    author_email and signs author_sig on the event it writes."""
    priv, pub = _keypair(tmp_path)
    rebar.create_identity("Bot", _BOT_EMAIL, keys=[pub], repo_root=str(store))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)
    ticket = _make_ticket(store)

    event, ticket_dir = _written_event(store, ticket)

    assert event.get("author_email") == _BOT_EMAIL
    assert event.get("author_id"), "expected author_id stamped from the resolved identity"
    assert event.get("author_sig"), "expected author_sig signed by the configured key"

    # The signature must actually VERIFY under authorship verification (not just be present):
    # strip author_sig to reconstruct the signed content, decode the DSSE envelope, verify.
    author_id = event["author_id"]
    sig = event.pop("author_sig")
    envelope = dsse.decode(sig)
    verdict = authorship.verify_event_authorship(event, envelope, author_id, repo_root=str(store))
    assert verdict.verified is True, f"author_sig did not verify: {verdict}"

    # And the ticket (a signed inbound event folded onto a real CREATE) still reduces cleanly.
    state = reduce_ticket(str(ticket_dir))
    assert state is not None and state.get("ticket_id") == ticket


def test_inbound_write_unsigned_without_key(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held-out edge: with an identity but NO signing key, the write still succeeds and is
    additive — author_email/author_id are stamped but author_sig is absent (best-effort, no
    raise)."""
    _, pub = _keypair(tmp_path)
    rebar.create_identity("Bot", _BOT_EMAIL, keys=[pub], repo_root=str(store))
    monkeypatch.delenv("REBAR_IDENTITY_SIGNING_KEY", raising=False)
    ticket = _make_ticket(store)

    event, _ = _written_event(store, ticket)

    assert event.get("author_email") == _BOT_EMAIL
    assert "author_sig" not in event  # no key ⇒ unsigned, never raises


def test_inbound_write_additive_no_identity(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Held-out edge: with NO identity resolving (git email matches no identity ticket), the
    write is unsigned and carries no author_id — the event still writes cleanly (additive)."""
    monkeypatch.delenv("REBAR_IDENTITY_SIGNING_KEY", raising=False)
    ticket = _make_ticket(store)
    subprocess.run(
        ["git", "config", "user.email", "stranger@example.com"],
        cwd=store,
        check=True,
        capture_output=True,
    )
    event, ticket_dir = _written_event(store, ticket)

    assert "author_id" not in event
    assert "author_sig" not in event
    assert event.get("event_type") == "COMMENT"  # the write itself still succeeds

    # Older-format round-trip: an event carrying no author_* keys folds cleanly through the
    # reducer (unknown/absent keys ignored — no format break).
    state = reduce_ticket(str(ticket_dir))
    assert state is not None and state.get("ticket_id") == ticket
