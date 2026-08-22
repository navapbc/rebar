"""An unreadable config is an ERROR for gated operations — the operator ruling on 39f8.

Operator ruling (ticket 39f8-ae7c-f651-4333): "Unreadable config should result in an
error." A malformed/corrupt ``rebar.toml`` must fail the gated operation loudly, naming
the parse fault — never silently resolve the ``verify.*`` gates to their defaults
(the retired fail-OPEN posture) and never mint a blocked-``unavailable`` verdict.

Driven end-to-end through the library surface (`rebar.claim`, `rebar.transition`) and the
CLI boundary, against a REAL store, so the tests pin what an operator actually sees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import config
from rebar.config import ConfigError

# `[verify` never closes its table header -> tomllib raises -> ConfigError.
_UNREADABLE = "[verify\nrequire_plan_review_for_close = true\n"


def _corrupt(repo: Path) -> None:
    (repo / "rebar.toml").write_text(_UNREADABLE, encoding="utf-8")
    config.reset_config_cache()


def test_config_error_is_re_exported_at_the_top_level_for_library_callers() -> None:
    # api-compat: rebar.claim/transition now raise ConfigError on an unreadable
    # config (operator ruling 39f8-ae7c), so the type must be catchable as
    # ``rebar.ConfigError`` without importing rebar.config.
    assert rebar.ConfigError is ConfigError
    assert "ConfigError" in rebar.__all__


def test_claim_on_an_unreadable_config_errors_and_leaves_the_ticket_open(
    rebar_repo: Path,
) -> None:
    tid = rebar.create_ticket("task", "claim under an unreadable config", repo_root=str(rebar_repo))
    _corrupt(rebar_repo)

    with pytest.raises(ConfigError) as excinfo:
        rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))

    message = str(excinfo.value)
    assert "config" in message, f"the error does not name the config fault: {message!r}"
    assert excinfo.value.__cause__ is not None, "the parse fault was not chained"
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open", (
        "a claim under an unreadable config changed ticket state"
    )


def test_close_on_an_unreadable_config_errors_and_leaves_the_ticket_in_progress(
    rebar_repo: Path,
) -> None:
    tid = rebar.create_ticket("task", "close under an unreadable config", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    _corrupt(rebar_repo)

    with pytest.raises(ConfigError) as excinfo:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))

    message = str(excinfo.value)
    assert "config" in message, f"the error does not name the config fault: {message!r}"
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "in_progress", (
        "a close under an unreadable config went through anyway"
    )


def test_the_cli_surfaces_the_config_fault_cleanly_not_as_a_traceback(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator-facing surface: `Error: ...` + exit 1, never a raw traceback."""
    tid = rebar.create_ticket(
        "task", "cli claim under an unreadable config", repo_root=str(rebar_repo)
    )
    monkeypatch.chdir(rebar_repo)
    _corrupt(rebar_repo)

    from rebar._cli import main

    # The clean-boundary property is that main() RETURNS an exit code (the ConfigError is
    # caught at the CLI boundary and rendered as an `Error:` line) rather than letting the
    # exception escape as an uncaught traceback — an escape would RAISE out of this call.
    code = main(["claim", tid])

    captured = capsys.readouterr()
    assert code != 0, "the CLI exited 0 on an unreadable config"
    assert "Error: cannot resolve" in captured.err, (
        f"stderr lacks the clean boundary rendering: {captured.err!r}"
    )
    assert "config" in captured.err, f"stderr does not name the config fault: {captured.err!r}"
