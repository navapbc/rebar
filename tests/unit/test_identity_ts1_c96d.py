"""Happy-path oracle for TS1 (c96d, epic gnu-whale-ichor transition): authorship
payload becomes an in-toto Statement.

The ONLY TS1 test the implementation sees. Pins the approved-design happy path:
``sign_event_authorship`` wraps an event in an in-toto Statement (subject binds
{event_uuid, content_hash}) inside a DSSE envelope, and ``verify_event_authorship``
verifies that against the author identity's in-band keys. Cross-identity / tamper /
malformed-Statement / primitive-preservation are held out.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.attest import authorship, dsse, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "dev@example.com"),
        ("git", "config", "user.name", "Dev"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def _event(author_id: str) -> dict:
    return {
        "uuid": "e1-1111-2222-3333",
        "event_type": "COMMENT",
        "author_id": author_id,
        "timestamp": "0000",
        "data": {"body": "hello"},
    }


def test_sign_event_authorship_wraps_in_toto_statement(store: Path, tmp_path: Path) -> None:
    """The DSSE payload is an in-toto Statement whose subject binds {event_uuid,
    content_hash}, with the pinned authorship payloadType."""
    priv, pub = _keypair(tmp_path, "alice")
    a = rebar.create_identity("Alice", "alice@example.com", keys=[pub], repo_root=str(store))
    event = _event(a)

    envelope = authorship.sign_event_authorship(event, priv, principal=a)
    assert isinstance(envelope, dsse.Envelope)
    assert envelope.payload_type == "application/vnd.rebar.authorship.v1+json"

    statement = json.loads(envelope.payload.decode("utf-8"))
    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert statement["predicateType"] == "application/vnd.rebar.authorship.v1+json"
    subject = statement["subject"][0]
    assert subject["name"] == event["uuid"]
    assert subject["digest"]["sha256"] == authorship.authorship_content_hash(event)


def test_verify_event_authorship_roundtrip(store: Path, tmp_path: Path) -> None:
    """An event signed by A verifies against A's in-band keys."""
    priv, pub = _keypair(tmp_path, "alice")
    a = rebar.create_identity("Alice", "alice@example.com", keys=[pub], repo_root=str(store))
    event = _event(a)

    envelope = authorship.sign_event_authorship(event, priv, principal=a)
    verdict = authorship.verify_event_authorship(event, envelope, a, repo_root=str(store))
    assert verdict.verified is True
