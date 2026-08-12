"""HELD-OUT oracle for bug ed5c — making a silent, identity-less writer LOUD.

Two independent silences let a review bot append ~8900 unsigned, ``Unknown``-authored events
for a month (bug beb1) with no signal anywhere:

* ``fsck``'s authorship line is a store-wide SUM, so one writer at 0% signed hides behind the
  volume of writers that are healthy;
* ``_seam._apply_authorship`` returns without a word when no identity resolves — only a signing
  FAILURE was ever logged, never a signing SKIP.

These tests pin the observable consequences of closing both, and — as much as the fix itself —
the FALSE-POSITIVE guard: a store's pre-signing history must stay quiet forever, or the check
gets muted by its own noise and buys nothing.
"""

from __future__ import annotations

import json
import logging

from rebar._commands import _seam
from rebar._commands.fsck_authorship import EnvAuthorshipTally

_ADOPTED = 2_000
_LATER = 3_000
_BEFORE = 1_000


def _event(env_id: str, ts: int, *, signed: bool = False, author: str | None = "Dev") -> dict:
    ev: dict = {"uuid": f"u{ts}-{env_id}", "env_id": env_id, "timestamp": ts}
    if signed:
        ev["author_sig"] = {"payloadType": "application/vnd.rebar.authorship+json"}
    if author is not None:
        ev["author"] = author
    return ev


def _findings(*events: tuple[str, dict]) -> list[str]:
    tally = EnvAuthorshipTally()
    for filename, payload in events:
        tally.observe(filename, payload)
    return tally.findings()


def test_active_env_that_signs_nothing_is_reported() -> None:
    """The beb1 shape: one env signs, a second writes alongside it and signs nothing."""
    out = _findings(
        ("a-CREATE.json", _event("good-env", _ADOPTED, signed=True)),
        ("b-CREATE.json", _event("silent-env", _LATER)),
        ("c-COMMENT.json", _event("silent-env", _LATER)),
    )
    assert len(out) == 1, out
    assert out[0].startswith("UNSIGNED_ENV: silent-env — ")
    assert "0 of 2 event(s) signed" in out[0]


def test_env_authoring_everything_unknown_is_reported() -> None:
    """Second signature of an identity-less writer: no author at all, or the placeholder."""
    for author in (None, "Unknown"):
        out = _findings(
            ("a-CREATE.json", _event("good-env", _ADOPTED, signed=True)),
            ("b-CREATE.json", _event("nameless", _LATER, author=author)),
        )
        assert len(out) == 1, (author, out)
        assert "authored 'Unknown'" in out[0]


def test_dormant_pre_signing_env_is_never_reported() -> None:
    """The false-positive guard. Every event predating signing adoption is unsigned and always
    will be; an env that stopped writing back then is history, not a defect. Without this the
    check would fire on every store with a past and be tuned out."""
    out = _findings(
        ("a-CREATE.json", _event("legacy-env", _BEFORE)),
        ("b-COMMENT.json", _event("legacy-env", _BEFORE)),
        ("c-CREATE.json", _event("good-env", _ADOPTED, signed=True)),
    )
    assert out == []


def test_store_that_never_adopted_signing_is_silent() -> None:
    """No signed event anywhere ⇒ signing is simply not in use; report nothing."""
    assert _findings(("a-CREATE.json", _event("only-env", _LATER))) == []


def test_partially_signed_env_is_not_reported() -> None:
    """An env that signs SOME events has a working identity — a transient gap is not this bug,
    and reporting it would drown the writers that sign nothing at all."""
    out = _findings(
        ("a-CREATE.json", _event("mixed", _ADOPTED, signed=True)),
        ("b-COMMENT.json", _event("mixed", _LATER)),
    )
    assert out == []


def test_snapshots_and_unattributable_payloads_are_ignored() -> None:
    """A SNAPSHOT is a derived projection of events already counted, and an event with no
    ``env_id`` names no writer — neither may invent or inflate a finding."""
    out = _findings(
        ("a-CREATE.json", _event("good-env", _ADOPTED, signed=True)),
        ("b-SNAPSHOT.json", _event("snap-env", _LATER)),
        ("c-CREATE.json", {"uuid": "x", "timestamp": _LATER}),
        ("d-CREATE.json", "not-a-dict"),
    )
    assert out == []


def test_findings_are_sorted_and_one_line_per_env() -> None:
    out = _findings(
        ("a-CREATE.json", _event("zzz", _LATER)),
        ("b-CREATE.json", _event("aaa", _LATER)),
        ("c-CREATE.json", _event("good", _ADOPTED, signed=True)),
    )
    assert [line.split()[1] for line in out] == ["aaa", "zzz"]


def test_fsck_json_output_counts_unsigned_env_as_an_issue() -> None:
    """AC2 is specifically that this is COUNTED, not another advisory line: it must survive the
    text→JSON transform as an issue and land in ``issue_count``."""
    from rebar._commands.fsck import _transform_json

    line = _findings(
        ("a-CREATE.json", _event("good-env", _ADOPTED, signed=True)),
        ("b-CREATE.json", _event("silent-env", _LATER)),
    )[0]
    payload = json.loads(_transform_json(f"authorship: 1 signed, 1 unsigned event(s)\n{line}"))
    kinds = [i["kind"] for i in payload["issues"]]
    assert "unsigned_env" in kinds
    assert payload["issue_count"] == len(payload["issues"]) >= 1
    # The line head is an env_id, NOT a ticket_id — it must not be parsed as one.
    entry = next(i for i in payload["issues"] if i["kind"] == "unsigned_env")
    assert "ticket_id" not in entry
    assert "silent-env" in entry["detail"]


class _RecordingConfig:
    """Minimal stand-in for the loaded config: signing configured, gate advisory (ADR-0045)."""

    class identity:
        require_authenticated = False
        signing_key = "/tmp/does-not-need-to-exist"


def test_seam_logs_when_signing_is_skipped_for_lack_of_identity(monkeypatch, caplog) -> None:
    """AC3. A key is configured but no identity resolves — previously a silent return."""
    monkeypatch.setattr(_seam.config, "load_config", lambda _root: _RecordingConfig())
    event = {"uuid": "ev-1"}

    with caplog.at_level(logging.WARNING, logger="rebar._commands._seam"):
        _seam._apply_authorship(event, "tid-1", "CREATE", {}, "/tracker", "/repo")

    assert "author_sig" not in event, "advisory gate must not start signing or failing writes"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "the signing-SKIPPED path must not be silent"
    message = warnings[0].getMessage()
    assert "SKIPPED" in message
    assert "ev-1" in message and "tid-1" in message
    assert "author_id" in message, "the log must name WHICH input was missing"


def test_seam_stays_quiet_when_signing_is_simply_not_configured(monkeypatch, caplog) -> None:
    """The overwhelming common case — no key, no gate — must stay on the silent fast path, or
    every write on an unsigned store emits a warning and the real signal is lost."""

    class _Unconfigured:
        class identity:
            require_authenticated = False
            signing_key = None

    monkeypatch.setattr(_seam.config, "load_config", lambda _root: _Unconfigured())
    with caplog.at_level(logging.WARNING, logger="rebar._commands._seam"):
        _seam._apply_authorship({"uuid": "ev-2"}, "tid-2", "CREATE", {}, "/tracker", "/repo")
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
