"""Positional-body commands must REJECT option-looking arguments, not store them (bug 00da).

`rebar comment` takes its body positionally and has no `--body`/`--body-file` option.
Before this guard, invoking one anyway stored the *flag token* as the body: the comment
became the literal 11-byte string ``--body-file`` (or the 6-byte ``--body``), the intended
content was silently discarded, and the command **exited 0** — so nothing signalled the
loss and a ``len(comments)`` check still passed. Two independent sessions lost their entire
durable record to this (live evidence: ticket b690 comments 5-9 with lengths 11/11/11/11/6,
and session log 55a9 comment 5).

The mechanism was in :func:`rebar._commands.main`'s arity path: it sliced
``args[: entry.min_args]`` and silently dropped the surplus, and never inspected a token for
a leading ``-``. These tests pin the repaired contract:

* an option-looking positional is a **usage error** (non-zero exit, message naming the
  correct form), never data;
* surplus positionals are a usage error too — no silent truncation;
* ``--`` is the escape hatch, so a body that legitimately starts with ``-`` is still
  writable, and ``--`` itself is consumed rather than stored as the body;
* the sibling positional-body commands audited by the ticket (``create`` / ``edit`` /
  ``session-log``) reject the same forms loudly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import main as commands_main

pytestmark = pytest.mark.unit

# The two forms that have actually cost sessions their work in the live store.
_REACHED_FOR_FORMS = ("--body-file", "--body")


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


@pytest.fixture
def ticket(rebar_repo: Path) -> str:
    return rebar.create_ticket("task", "subject", repo_root=str(rebar_repo))


def _bodies(ticket_id: str, repo: Path) -> list[str]:
    shown = rebar.show_ticket(ticket_id, repo_root=str(repo))
    return [c.get("body", "") for c in shown["comments"]]


def _tags(ticket_id: str, repo: Path) -> list[str]:
    return list(rebar.show_ticket(ticket_id, repo_root=str(repo))["tags"])


# --------------------------------------------------------------------------- comment


@pytest.mark.parametrize("flag", _REACHED_FOR_FORMS)
def test_comment_rejects_the_forms_that_lost_real_work(
    flag: str,
    ticket: str,
    rebar_repo: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`--body-file <path>` / `--body <text>` must fail loudly, not be stored as the body."""
    payload = tmp_path / "analysis.md"
    payload.write_text("# root cause\nthe real content\n", encoding="utf-8")
    value = str(payload) if flag == "--body-file" else "the real content"

    rc = commands_main(["comment", ticket, flag, value])

    assert rc != 0, f"{flag} was accepted — the body would be silently replaced by the flag token"
    # The regression signature: the flag token itself must never reach the store.
    assert _bodies(ticket, rebar_repo) == []
    err = capsys.readouterr().err
    assert flag in err, "the error must name the offending argument"
    assert "rebar comment <ticket_id> <body>" in err, "the error must name the correct form"


def test_comment_error_points_at_the_long_body_workaround(
    ticket: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is no `--body-file`; the message must say how to pass a long body instead.

    This is what redirects a session that reached for `--body-file` — the whole reason the
    bug cost work twice was that nothing told them the form was wrong.
    """
    commands_main(["comment", ticket, "--body-file", "/tmp/x.md"])
    err = capsys.readouterr().err
    assert '"$(cat ' in err, "the message must show the cat-substitution form for long bodies"
    assert "--" in err, "the message must mention the -- escape hatch"


def test_comment_rejects_surplus_positionals_instead_of_dropping_them(
    ticket: str, rebar_repo: Path
) -> None:
    """No silent truncation: an extra positional is a usage error, not a discarded token.

    Dropping the surplus is half the mechanism — it is what let `--body-file <path>` exit 0
    with the path thrown away.
    """
    rc = commands_main(["comment", ticket, "first", "second"])
    assert rc != 0
    assert _bodies(ticket, rebar_repo) == []


def test_comment_stores_an_ordinary_body(ticket: str, rebar_repo: Path) -> None:
    """The guard must not disturb the normal path."""
    assert commands_main(["comment", ticket, "an ordinary note"]) == 0
    assert _bodies(ticket, rebar_repo) == ["an ordinary note"]


# ------------------------------------------------------------------- the -- escape hatch


def test_double_dash_lets_a_dash_leading_body_through(ticket: str, rebar_repo: Path) -> None:
    """`rebar comment <id> -- "-starts with a dash"` must store the body verbatim.

    Before the fix this stored the separator itself (`--`, 2 bytes) and dropped the body —
    the same silent-loss shape as the reported bug.
    """
    assert commands_main(["comment", ticket, "--", "-starts with a dash"]) == 0
    assert _bodies(ticket, rebar_repo) == ["-starts with a dash"]


def test_double_dash_does_not_smuggle_an_option_name_into_the_store(
    ticket: str, rebar_repo: Path
) -> None:
    """After `--`, a flag-looking token is *data* — that is the point of the escape hatch."""
    assert commands_main(["comment", ticket, "--", "--body-file"]) == 0
    assert _bodies(ticket, rebar_repo) == ["--body-file"]


def test_bare_double_dash_with_no_body_is_a_usage_error(ticket: str, rebar_repo: Path) -> None:
    """`--` is a separator, never the body itself."""
    assert commands_main(["comment", ticket, "--"]) != 0
    assert _bodies(ticket, rebar_repo) == []


# ------------------------------------------------------- the other arity-path commands


def test_tag_rejects_an_option_looking_value(ticket: str, rebar_repo: Path) -> None:
    """`tag` shares comment's arity path, so it shared the silent-loss bug."""
    assert commands_main(["tag", ticket, "--body-file", "/tmp/x.md"]) != 0
    assert _tags(ticket, rebar_repo) == []


def test_tag_double_dash_allows_a_dash_leading_tag(ticket: str, rebar_repo: Path) -> None:
    assert commands_main(["tag", ticket, "--", "-dash-tag"]) == 0
    assert _tags(ticket, rebar_repo) == ["-dash-tag"]


def test_untag_rejects_an_option_looking_value(ticket: str) -> None:
    assert commands_main(["untag", ticket, "--body-file", "/tmp/x.md"]) != 0


def test_archive_rejects_an_option_looking_ticket_id() -> None:
    """The guard covers every positional slot, not just the body slot."""
    assert commands_main(["archive", "--body-file"]) != 0


# --------------------------------------------------------- audited sibling commands


@pytest.mark.parametrize("flag", _REACHED_FOR_FORMS)
def test_create_rejects_an_unknown_option_instead_of_treating_it_as_parent(
    flag: str, rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`create` swallowed any unrecognised token into `parent`, so a mistyped option
    surfaced as the baffling "parent ticket '--body-file' does not exist"."""
    rc = commands_main(["create", "task", "title", flag, "value"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "parent ticket" not in err, "an unknown option must not be reported as a bad parent"
    assert flag in err


@pytest.mark.parametrize("flag", _REACHED_FOR_FORMS)
def test_edit_rejects_the_reached_for_forms(flag: str, ticket: str) -> None:
    """Regression pin: `edit` already rejects unknown fields — keep it that way."""
    assert commands_main(["edit", ticket, flag, "value"]) != 0


@pytest.mark.parametrize("flag", _REACHED_FOR_FORMS)
def test_session_log_append_rejects_the_reached_for_forms(flag: str, rebar_repo: Path) -> None:
    """Regression pin: `session-log` already rejects unknown options — keep it that way."""
    assert commands_main(["session-log", "append", flag, "value"]) != 0


def test_session_log_append_double_dash_allows_a_dash_leading_entry(rebar_repo: Path) -> None:
    """`session-log` rejected every dash-leading entry with no way to write one."""
    assert commands_main(["session-log", "append", "--", "-dash entry"]) == 0
