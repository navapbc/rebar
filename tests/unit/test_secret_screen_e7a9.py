"""The event-write seam must REFUSE secret-bearing bodies (bug e7a9).

On 2026-08-03 a session posted a comment carrying a full environment dump with seven
live credentials. GitHub push protection then rejected ``refs/heads/tickets`` (GH013),
so EVERY session's store writes queued local-only — a full store-sharing outage caused
by one comment. rebar itself had no write-time guard: ``append_event`` accepted any
body, and the only control that stopped the leak lived outside this project.

The operator decision (recorded on e7a9, 2026-08-07) is **REFUSE, with an allowed force
override**. Redaction was rejected: silently rewriting a caller's payload makes the
stored event differ from what the caller believes it wrote, and in an event-sourced
store that divergence is unrecoverable. These tests pin the resulting contract:

* a live-shaped credential in a comment / description / edit body is refused and the
  event does **not** land;
* the refusal names WHICH detector fired and WHERE, so the override is an informed
  choice rather than a blind retry;
* the refusal never echoes the secret — not in the exception, not on stderr, not in
  the log records, not on disk. A guard that leaks while reporting is worse than none;
* benign look-alikes (truncated placeholders, regex literals — the corpus says these
  are the COMMON case, and a naive screen would have refused e7a9's own filing) pass;
* ``--allow-secret-pattern=<reason>`` lands the write and records it as forced (who,
  why) so a forced write is auditable and distinguishable from a clean one.

Every credential-shaped string in this file is SYNTHESISED at runtime by
:func:`_fake_secret` — no live-shaped literal is committed, so this suite cannot itself
trip a secret scanner (or become the leak it guards against).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from _topology_template import clone_topology_template

import rebar
from rebar._commands import leaf
from rebar._commands._seam import CommandError

pytestmark = pytest.mark.unit

_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _fake_secret(prefix: str, length: int, alphabet: str = _ALPHABET) -> str:
    """A deterministic high-entropy body of *length* chars behind *prefix*.

    Built at runtime so no live-shaped credential literal is ever committed to this
    repo. The body cycles a 62-char alphabet, so its Shannon entropy clears any
    entropy floor the screen applies just as a real key's would.
    """
    body = "".join(alphabet[(i * 17 + 5) % len(alphabet)] for i in range(length))
    return prefix + body


def _pem_banner(kind: str) -> str:
    """A PEM begin-banner assembled at runtime rather than held as a source literal.

    Same discipline as :func:`_fake_secret`, for the same reason: the repo-wide secret
    scanner cannot tell a test fixture's banner from a real one, and flagged the literal
    form here. It must NOT be silenced with a scanner allowlist — measured behaviour is
    that once the FIRST private-key match in a file is allowlisted, a genuine key later
    in that same file is no longer reported, so an allowlist entry keyed on this fixture
    would disarm the detector on precisely the file that must stay armed. Assembling the
    banner leaves the scanner fully active here and still exercises the shipped pattern,
    which matches the banner text, not the source spelling.
    """
    return "-----BEGIN " + kind + " PRIVATE KEY" + "-----"


# The seven families the incident dump actually carried, plus the shapes the ticket's
# fix plan enumerates. Each entry: (family substring expected in the refusal, value).
_LIVE_SHAPES: tuple[tuple[str, str], ...] = (
    ("Anthropic", _fake_secret("sk-ant-api03-", 95)),
    ("GitHub", _fake_secret("ghp_", 36)),
    ("GitHub", _fake_secret("gho_", 36)),
    ("GitHub", _fake_secret("github_pat_", 82)),
    ("Google", _fake_secret("AIza", 35)),
    ("Atlassian", _fake_secret("ATATT3xFfGF0", 180)),
    ("Slack", _fake_secret("xoxb-", 40, "0123456789")),
    ("AWS", _fake_secret("AKIA", 16, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")),
    ("PyPI", _fake_secret("pypi-AgEIcHlwaS5vcmc", 120)),
    ("OpenAI", _fake_secret("sk-proj-", 80)),
    ("OpenAI", _fake_secret("sk-svcacct-", 80)),
    # A real paste puts the PEM header on its own line; prose enumerating headers inline
    # does not, which is what the anchored pattern separates (see _BENIGN below).
    ("private key", f"\n{_pem_banner('OPENSSH')}\nb3BlbnNzaC1rZXk\n"),
)

# Verbatim benign strings from the live store's own sweep (recorded on e7a9): a
# truncated placeholder and a REGEX LITERAL, both in prose DESCRIBING the screen. Two
# of the five real matches were e7a9's own CREATE and EDIT events, so a screen that
# fires here refuses the filing of the very bug it implements.
_BENIGN: tuple[str, ...] = (
    "sk-ant-api03-...'), Bearer headers, and request-body",
    "sk-ant-api03-[A-Za-z0-9_-]{93,}) and Bearer token pa",
    "reject bodies matching sk-ant-*, ghp_/gho_/ghu_/ghs_/ghr_, github_pat_, AIza...,",
    "ATATT..., xox?-, AKIA..., pypi-AgE..., sk-proj-/sk-svcacct-, PEM private-key headers",
    "The regex is ghp_[A-Za-z0-9]{36} and github_pat_[A-Za-z0-9_]{82}.",
    "export ANTHROPIC_API_KEY=$(cat ~/.anthropic-key)  # never inline the value",
    # A real would-refuse from the live store (ticket 401a): prose ENUMERATING PEM headers.
    f"matching any of: `{_pem_banner('OPENSSH')}`, `{_pem_banner('RSA')}`",
)


def _init_secret_repo(repo: Path) -> None:
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))


@pytest.fixture(scope="session")
def _secret_repo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("secret-repo-template")
    repo = root / "repo"
    from rebar import config as _config

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("REBAR_ROOT", str(repo))
        patch.setenv("XDG_CONFIG_HOME", str(root / "xdg-empty"))
        for variable in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
            patch.delenv(variable, raising=False)
        _config.reset_config_cache()
        try:
            _init_secret_repo(repo)
        finally:
            _config.reset_config_cache()
    return repo


@pytest.fixture
def rebar_repo(
    _secret_repo_template: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    repo = clone_topology_template(_secret_repo_template, tmp_path / "repo")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "gate"))
    from rebar import config as _config
    from rebar._store import ensures as _ensures

    _config.reset_config_cache()
    _ensures._reset_pending_cache()
    try:
        yield repo
    finally:
        _config.reset_config_cache()
        _ensures._reset_pending_cache()


@pytest.fixture
def ticket(rebar_repo: Path) -> str:
    return rebar.create_ticket("task", "subject", repo_root=str(rebar_repo))


def _tracker(repo: Path) -> Path:
    """The real tracker dir (``.tickets-tracker``), resolved the way rebar resolves it.

    Hard-coding a guess here once made the on-disk assertions vacuous — they globbed a
    directory that does not exist and trivially passed.
    """
    from rebar._commands._seam import tracker_dir

    resolved = Path(tracker_dir(str(repo)))
    assert resolved.is_dir(), f"tracker dir not found at {resolved}"
    return resolved


def _store_text(repo: Path) -> str:
    """Every byte the repo holds, as one string (store + working tree + git objects)."""
    chunks = []
    for path in repo.rglob("*"):
        if path.is_file():
            try:
                chunks.append(path.read_text(errors="replace"))
            except OSError:  # pragma: no cover - defensive
                continue
    return "\n".join(chunks)


def _comments(ticket_id: str, repo: Path) -> list[dict]:
    return list(rebar.show_ticket(ticket_id, repo_root=str(repo))["comments"])


# --------------------------------------------------------------------------- refuse


_LIVE_IDS = [f"{family}-{i}" for i, (family, _) in enumerate(_LIVE_SHAPES)]


@pytest.mark.parametrize("family,value", _LIVE_SHAPES, ids=_LIVE_IDS)
def test_live_shaped_credential_in_a_comment_is_refused(
    family: str, value: str, ticket: str, rebar_repo: Path
) -> None:
    """The write is REFUSED — the event does not land (operator decision, 2026-08-07)."""
    with pytest.raises(CommandError):
        leaf.comment(ticket, f"here is the env dump\nKEY={value}\n", repo_root=str(rebar_repo))

    assert _comments(ticket, rebar_repo) == [], "a refused write must not land an event"
    assert value not in _store_text(rebar_repo), "the secret must never reach disk"


def test_refusal_names_which_detector_fired_and_where(ticket: str, rebar_repo: Path) -> None:
    """An informed override needs the family AND the location, not just 'denied'."""
    value = _fake_secret("sk-ant-api03-", 95)
    body = "line one\nline two\nKEY=" + value + "\n"

    with pytest.raises(CommandError) as excinfo:
        leaf.comment(ticket, body, repo_root=str(rebar_repo))

    message = str(excinfo.value)
    assert "Anthropic" in message, "the refusal must name the matched family"
    assert "body" in message, "the refusal must name the field that matched"
    assert "line 3" in message or ":3" in message, "the refusal must name the line"
    assert "--allow-secret-pattern" in message, "the refusal must teach the override"


def test_refusal_never_echoes_the_secret_anywhere(
    ticket: str,
    rebar_repo: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A guard that leaks the secret while reporting it is worse than no guard.

    The value must appear in NO channel the refusal touches: the exception message,
    stdout, stderr, the log records, or the store on disk. The 12-char prefix is
    checked too, so a 'redact all but the first few chars' helper cannot sneak in.
    """
    value = _fake_secret("sk-ant-api03-", 95)
    tail = value[-12:]
    head = value[len("sk-ant-api03-") :][:12]

    with caplog.at_level(0):
        with pytest.raises(CommandError) as excinfo:
            leaf.comment(ticket, f"KEY={value}", repo_root=str(rebar_repo))

    captured = capsys.readouterr()
    channels = {
        "exception": str(excinfo.value),
        "stdout": captured.out,
        "stderr": captured.err,
        "logs": "\n".join(r.getMessage() for r in caplog.records),
        "store": _store_text(rebar_repo),
    }
    for name, text in channels.items():
        assert value not in text, f"the secret leaked into {name}"
        assert tail not in text, f"a tail fragment of the secret leaked into {name}"
        assert head not in text, f"a head fragment of the secret leaked into {name}"


def test_description_and_edit_bodies_are_screened_too(rebar_repo: Path) -> None:
    """CREATE and EDIT carry free text as well — the seam screens every field."""
    value = _fake_secret("ghp_", 36)

    with pytest.raises((CommandError, rebar.RebarError)) as create_exc:
        rebar.create_ticket(
            "task", "leaky", description=f"token {value}", repo_root=str(rebar_repo)
        )
    assert "GitHub token" in str(create_exc.value), "must fail ON the screen, not incidentally"

    clean = rebar.create_ticket("task", "clean", repo_root=str(rebar_repo))
    with pytest.raises((CommandError, rebar.RebarError)) as edit_exc:
        rebar.edit_ticket(clean, description=f"token {value}", repo_root=str(rebar_repo))
    assert "GitHub token" in str(edit_exc.value)
    assert value not in _store_text(rebar_repo)


# --------------------------------------------------------------------------- benign


@pytest.mark.parametrize("body", _BENIGN)
def test_benign_look_alikes_are_not_refused(body: str, ticket: str, rebar_repo: Path) -> None:
    """Truncated placeholders and regex literals must pass, or the screen cannot be
    left on by default — and e7a9's own filing would have been refused."""
    leaf.comment(ticket, body, repo_root=str(rebar_repo))
    assert [c["body"] for c in _comments(ticket, rebar_repo)] == [body]


def test_this_tickets_own_description_is_writable(ticket: str, rebar_repo: Path) -> None:
    """The regression the corpus sweep found: 2 of 5 live matches were e7a9's own
    CREATE and EDIT events. Writing up a credential incident must stay possible."""
    body = "\n".join(_BENIGN)
    leaf.comment(ticket, body, repo_root=str(rebar_repo))
    assert len(_comments(ticket, rebar_repo)) == 1


# --------------------------------------------------------------------------- override


def test_force_override_lands_the_write_and_records_it_as_forced(
    ticket: str, rebar_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A false positive must never permanently block a legitimate write — and the
    forced write must be auditable, i.e. distinguishable from a clean one."""
    value = _fake_secret("sk-ant-api03-", 95)
    reason = "documenting the incident; value is a synthetic fixture"

    with caplog.at_level(0):
        leaf.comment(
            ticket,
            f"KEY={value}",
            repo_root=str(rebar_repo),
            allow_secret_pattern=reason,
        )

    # The forced path is the ONLY path that logs, so it is the one that must be checked
    # for a leak — the refusal path emits no records at all.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "FORCED" in logged and "Anthropic API key" in logged
    assert value not in logged and value[-12:] not in logged

    comments = _comments(ticket, rebar_repo)
    assert len(comments) == 1, "the forced write must land"

    tracker = _tracker(rebar_repo)
    events = [json.loads(p.read_text()) for p in sorted((tracker / ticket).glob("*-COMMENT.json"))]
    assert len(events) == 1
    override = events[0]["data"].get("secret_override")
    assert override, "a forced write must be recorded as forced"
    assert override["reason"] == reason, "the override must record WHY"
    assert events[0]["author"], "the event's author records WHO forced it"
    assert any("Anthropic" in f for f in override["families"]), (
        "the override must record which detector was bypassed"
    )
    assert value not in json.dumps(override), "the audit record must not echo the secret"
    # Positive control for the on-disk assertions used by the refusal tests: a write that
    # DOES land puts the value in _store_text, so "value not in _store_text" is a real
    # observation there and not a vacuous one.
    assert value in _store_text(rebar_repo)


def test_a_clean_write_carries_no_override_marker(ticket: str, rebar_repo: Path) -> None:
    """Distinguishability runs both ways: a clean write is unmarked."""
    leaf.comment(ticket, "nothing to see here", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)
    event = json.loads(next((tracker / ticket).glob("*-COMMENT.json")).read_text())
    assert "secret_override" not in event["data"]


def test_cli_refuses_then_accepts_with_the_override(
    ticket: str, rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end over the CLI dispatcher: refuse loudly, then honour the override.

    The override uses the ``--flag=<reason>`` form so the positional-body guard (bug
    00da) keeps rejecting every OTHER option-looking token as a usage error.
    """
    from rebar._commands import main as commands_main

    value = _fake_secret("ghp_", 36)

    assert commands_main(["comment", ticket, f"KEY={value}"]) != 0
    err = capsys.readouterr().err
    assert "GitHub" in err and value not in err
    assert _comments(ticket, rebar_repo) == []

    assert (
        commands_main(
            ["comment", ticket, f"KEY={value}", "--allow-secret-pattern=synthetic fixture"]
        )
        == 0
    )
    assert len(_comments(ticket, rebar_repo)) == 1


def test_the_override_is_not_exposed_over_mcp() -> None:
    """The escape hatch is a human operator's judgment call, so it stays CLI-only.

    MCP tool schemas are derived from the tool functions' signatures in
    ``rebar._mcp_writes``, so the absence of the parameter there IS the absence of the
    override from every published schema.
    """
    import inspect

    from rebar import _mcp_llm, _mcp_writes

    for module in (_mcp_writes, _mcp_llm):
        source = inspect.getsource(module)
        assert "allow_secret_pattern" not in source, f"override reached {module.__name__}"
        assert "allow-secret-pattern" not in source, f"override reached {module.__name__}"
    # Pin the positional-only call so a future `**kwargs` passthrough cannot smuggle the
    # override into the tool schema without failing here.
    assert "rebar.comment(ticket_id, body)" in inspect.getsource(_mcp_writes)


def test_cli_override_reaches_the_self_parsing_composers(rebar_repo: Path) -> None:
    """`create`/`edit`/`session-log` parse their own argv — the override must still reach
    them, or the refusal advertises a recovery step that does not exist and a false
    positive on a description becomes a permanent hard block."""
    from rebar._commands import main as commands_main

    value = _fake_secret("ghp_", 36)

    assert commands_main(["create", "task", "leaky", f"--description=tok {value}"]) != 0
    assert (
        commands_main(
            [
                "create",
                "task",
                "leaky",
                f"--description=tok {value}",
                "--allow-secret-pattern=synthetic fixture",
            ]
        )
        == 0
    )


def test_the_escape_hatch_still_yields_a_literal_body(ticket: str, rebar_repo: Path) -> None:
    """Everything after `--` is data (bug 00da) — including a body that happens to start
    with the override flag, e.g. the documentation of this feature."""
    from rebar._commands import main as commands_main

    body = "--allow-secret-pattern=<reason> is how you force a write"
    assert commands_main(["comment", ticket, "--", body]) == 0
    assert [c["body"] for c in _comments(ticket, rebar_repo)] == [body]


def test_padding_after_a_key_cannot_dilute_the_entropy_floor() -> None:
    """Fail-open regression: with a greedy quantifier, same-class padding after a real
    key (an ASCII rule in a log, ``-`` x400) dragged whole-match entropy under the floor
    and the screen missed a genuine credential. The window is fixed-width for this reason."""
    from rebar.secret_screen import scan_text

    for key in (_fake_secret("sk-ant-api03-", 95), _fake_secret("ghp_", 36)):
        for padding in ("a" * 500, "-" * 400, "0" * 400, ""):
            assert scan_text(f"KEY={key}{padding}"), f"missed a key padded with {padding[:1]!r}x"


def test_a_credential_shaped_payload_key_is_flagged_without_being_echoed() -> None:
    """The field path is printed by the refusal, so a payload KEY is a leak channel too."""
    from rebar.secret_screen import refusal_message, screen_event_data

    value = _fake_secret("sk-ant-api03-", 95)
    findings = screen_event_data({value: "harmless"})
    assert findings, "a credential used as a payload key must be detected"
    message = refusal_message(findings, override_flag="--allow-secret-pattern")
    assert value not in message and value[20:40] not in message


def test_the_reconciler_and_importer_paths_are_screened_too(rebar_repo: Path) -> None:
    """The writers that compose their own events (inbound Jira translate, the txn/delete
    cores, the NDJSON importer) reach the store WITHOUT ``append_event``. The screen lives
    at ``finalize_event`` — the seam all of them DO share — so none of them is a bypass."""
    from rebar._commands import _seam

    value = _fake_secret("ghp_", 36)
    event: dict = {"event_type": "COMMENT"}
    data = {"body": f"from jira: {value}"}

    with pytest.raises(CommandError):
        _seam.finalize_event(event, "t1", "COMMENT", data, None, str(rebar_repo))

    # ... and the same seam honours an in-scope override.
    with _seam.forced_secret_write("synthetic fixture"):
        _seam.screen_event(data)
    assert data["secret_override"]["reason"] == "synthetic fixture"


def test_the_inbound_jira_translate_routes_through_the_shared_seam() -> None:
    """Jira comment bodies are UNTRUSTED external input and the store auto-pushes.

    The reconciler used to call ``attribution_fields`` + ``_apply_authorship`` directly —
    the two halves of ``finalize_event`` — which silently skipped anything added to the
    seam. Pin the routing so it cannot regress back into a bypass.
    """
    import rebar

    source = (
        Path(rebar.__file__).parent / "_engine" / "rebar_reconciler" / "inbound_translate.py"
    ).read_text()
    assert "_seam.finalize_event(" in source
    assert "_seam._apply_authorship(" not in source


def test_override_requires_a_reason(ticket: str, rebar_repo: Path) -> None:
    """An empty reason is not an override — it would defeat the audit trail."""
    value = _fake_secret("ghp_", 36)
    with pytest.raises(CommandError):
        leaf.comment(ticket, f"KEY={value}", repo_root=str(rebar_repo), allow_secret_pattern="")
    assert _comments(ticket, rebar_repo) == []


# ------------------------------------------------------- clear-text-logging (alerts 83-87)


def test_the_cli_forced_write_log_names_the_family_and_not_the_secret(
    ticket: str, rebar_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The CLI forced-write audit log must name WHICH detector was bypassed, never WHAT.

    CodeQL flags this log site (``py/clear-text-logging-sensitive-data``) because the
    logged expression reads out of the event ``data`` payload, which by construction may
    carry a credential shape — that is what the screen just detected. What it actually
    reads is ``secret_override["families"]``, assembled by
    :func:`rebar.secret_screen.override_record` from ``SecretFinding.family``, a literal
    from the module-level pattern table; :class:`~rebar.secret_screen.SecretFinding` has
    no value field at all. That makes the alert a taint over-approximation rather than a
    leak — and this test is what keeps it one, so a future edit cannot start logging the
    value (or a prefix of it) without failing here.

    The library route (``allow_secret_pattern=`` passed to ``append_event``) is pinned by
    ``test_force_override_lands_the_write_and_records_it_as_forced``; this covers the
    OTHER site, the CLI route through ``forced_secret_write`` -> ``screen_event``.
    """
    from rebar._commands import main as commands_main

    value = _fake_secret("sk-ant-api03-", 95)
    head, tail = value[len("sk-ant-api03-") :][:12], value[-12:]

    with caplog.at_level(0):
        assert (
            commands_main(
                [
                    "comment",
                    ticket,
                    f"KEY={value}",
                    "--allow-secret-pattern=synthetic fixture",
                ]
            )
            == 0
        )

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "FORCED" in logged, "the forced write must still be announced"
    assert "Anthropic API key" in logged, (
        "the log must keep naming which detector was bypassed — redacting the family "
        "would remove the diagnostic value without removing any secret"
    )
    fragments = (
        (value, "the secret"),
        (head, "a head fragment"),
        (tail, "a tail fragment"),
    )
    for fragment, label in fragments:
        assert fragment not in logged, f"{label} leaked into the CLI forced-write log"
    assert len(_comments(ticket, rebar_repo)) == 1, "the forced write must still land"


def test_usage_errors_never_echo_the_override_reason(
    ticket: str, rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The three CLI usage-error prints must not carry the ``--allow-secret-pattern`` value.

    CodeQL's sensitive-data heuristic flags :func:`rebar._commands._extract_allow_secret`
    on its NAME and taints its whole return tuple, so plain argv inherits a "secret"
    label and every downstream print is reported. Two things make that false, and this
    test pins both: the flag token is STRIPPED from the returned argv, so its value
    provably cannot reach these prints; and each print still names the argument it
    rejected, which is the whole diagnostic point of the message.
    """
    from rebar._commands import main as commands_main

    sentinel = "override-reason-sentinel-must-not-be-echoed"
    flag = f"--allow-secret-pattern={sentinel}"

    cases = (
        ("unknown command", ["nosuchcommand", flag], "nosuchcommand"),
        ("unrecognised option", ["comment", ticket, "body", "--bogus", flag], "--bogus"),
        ("surplus positional", ["comment", ticket, "body", "surplus", flag], "surplus"),
    )
    for label, argv, expected_echo in cases:
        commands_main(list(argv))
        captured = capsys.readouterr()
        for channel, text in (("stdout", captured.out), ("stderr", captured.err)):
            assert sentinel not in text, f"the override reason leaked into {channel} ({label})"
        assert expected_echo in captured.err, (
            f"the {label} usage error must still name the argument it rejected"
        )
