"""b5db: the interactive `rebar jira-onboard` wizard + its config writer.

Two surfaces under test:

  * ``rebar.config.write_jira_config`` — the parse(tomllib)->mutate->re-emit writer
    that persists url/user/project to a REBAR-OWNED ``rebar.toml`` ``[jira]`` section,
    NEVER editing a user ``pyproject.toml`` and NEVER writing the secret token.
  * ``rebar._cli._jira_onboard.jira_onboard`` — detect / prompt / persist / validate,
    routed through ``rebar._cli.main(["jira-onboard", ...])``.

The bridge-probe subprocess is never actually launched (no live Jira): tests either
pass ``--no-validate`` or run with ``JIRA_API_TOKEN`` absent (probe skipped), or
monkeypatch ``_bridge_probe`` to assert the env overlay.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

from rebar import config as cfg
from rebar._cli import main

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REBAR_CONFIG",
        "XDG_CONFIG_HOME",
        "REBAR_ROOT",
        "JIRA_URL",
        "JIRA_USER",
        "JIRA_PROJECT",
        "JIRA_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg.reset_config_cache()


def _proj(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    monkeypatch.setenv("REBAR_ROOT", str(p))
    return p


# ── write_jira_config: create / upsert / preserve ──────────────────────────────
def test_write_creates_rebar_toml_when_none_exists(tmp_path, monkeypatch) -> None:
    p = _proj(tmp_path, monkeypatch)
    target = cfg.write_jira_config("https://x.atlassian.net", "me@x.com", "DIG")
    assert target == p / "rebar.toml"
    cfg.reset_config_cache()
    jira = cfg.load_config().jira
    assert (jira.url, jira.user, jira.project) == ("https://x.atlassian.net", "me@x.com", "DIG")


def test_write_preserves_other_sections(tmp_path, monkeypatch) -> None:
    p = _proj(tmp_path, monkeypatch)
    (p / "rebar.toml").write_text(
        '[tracker]\nbranch = "tickets"\n\n[jira]\nurl = "old"\n', encoding="utf-8"
    )
    cfg.write_jira_config("https://new", "u2", "P2")
    data = tomllib.loads((p / "rebar.toml").read_text(encoding="utf-8"))
    assert data["tracker"]["branch"] == "tickets"  # untouched
    assert data["jira"] == {"url": "https://new", "user": "u2", "project": "P2"}


def test_write_when_only_pyproject_creates_rebar_toml_and_leaves_pyproject_untouched(
    tmp_path, monkeypatch
) -> None:
    p = _proj(tmp_path, monkeypatch)
    pyproject = p / "pyproject.toml"
    body = '[tool.rebar.jira]\nurl = "https://pp"\n# a user comment\n'
    pyproject.write_text(body, encoding="utf-8")
    target = cfg.write_jira_config("https://fresh", "u", "P")
    assert target == p / "rebar.toml"  # fresh rebar.toml, NOT the pyproject
    assert pyproject.read_text(encoding="utf-8") == body  # byte-for-byte unchanged
    cfg.reset_config_cache()
    # rebar.toml wins read precedence over pyproject.
    assert cfg.load_config().jira.url == "https://fresh"


def test_write_round_trips_inline_table_without_duplicate_section(tmp_path, monkeypatch) -> None:
    p = _proj(tmp_path, monkeypatch)
    (p / "rebar.toml").write_text('jira = { url = "inline", user = "iu" }\n', encoding="utf-8")
    cfg.write_jira_config("https://final", "uf", "PF")
    txt = (p / "rebar.toml").read_text(encoding="utf-8")
    # Must still be valid TOML with exactly one jira table (no appended duplicate).
    data = tomllib.loads(txt)
    assert data["jira"] == {"url": "https://final", "user": "uf", "project": "PF"}


def test_write_round_trips_dotted_key_form(tmp_path, monkeypatch) -> None:
    p = _proj(tmp_path, monkeypatch)
    (p / "rebar.toml").write_text('jira.url = "dotted"\njira.user = "du"\n', encoding="utf-8")
    cfg.write_jira_config("https://d2", "du2", "DP")
    data = tomllib.loads((p / "rebar.toml").read_text(encoding="utf-8"))
    assert data["jira"] == {"url": "https://d2", "user": "du2", "project": "DP"}


def test_write_never_persists_the_secret_token(tmp_path, monkeypatch) -> None:
    p = _proj(tmp_path, monkeypatch)
    monkeypatch.setenv("JIRA_API_TOKEN", "super-secret")
    cfg.write_jira_config("https://x", "u", "P")
    raw = (p / "rebar.toml").read_text(encoding="utf-8")
    assert "super-secret" not in raw
    assert "api_token" not in raw
    assert "token" not in raw


def test_write_malformed_existing_is_fail_closed(tmp_path, monkeypatch) -> None:
    p = _proj(tmp_path, monkeypatch)
    bad = "[jira]\nurl = [ broken toml\n"
    (p / "rebar.toml").write_text(bad, encoding="utf-8")
    with pytest.raises(cfg.ConfigError):
        cfg.write_jira_config("https://x", "u", "P")
    # Nothing written — the malformed file is left as-is.
    assert (p / "rebar.toml").read_text(encoding="utf-8") == bad


def test_write_round_trips_a_float_config_value(tmp_path, monkeypatch) -> None:
    """A full re-emit must not corrupt non-jira scalar types — e.g. the float
    `verify.verify_window_headroom` — by stringifying them."""
    p = _proj(tmp_path, monkeypatch)
    (p / "rebar.toml").write_text(
        "[verify]\nverify_window_headroom = 0.8\n\n[jira]\nurl = 'x'\n", encoding="utf-8"
    )
    cfg.write_jira_config("https://x", "u", "P")
    data = tomllib.loads((p / "rebar.toml").read_text(encoding="utf-8"))
    assert data["verify"]["verify_window_headroom"] == 0.8  # still a float, not "0.8"
    # And the round-tripped file still loads through the typed loader.
    cfg.reset_config_cache()
    assert cfg.load_config().verify.verify_window_headroom == 0.8


def test_emit_toml_fails_closed_on_unsupported_type() -> None:
    """An unsupported value type raises rather than silently mis-emitting."""
    import datetime

    with pytest.raises(cfg.ConfigError):
        cfg._emit_toml({"jira": {"when": datetime.datetime(2020, 1, 1)}})
    with pytest.raises(cfg.ConfigError):
        cfg._emit_toml({"jira": {"nested": {"k": "v"}}})  # nested sub-table


def test_clear_removes_only_the_jira_keys(tmp_path, monkeypatch) -> None:
    p = _proj(tmp_path, monkeypatch)
    (p / "rebar.toml").write_text(
        '[tracker]\nbranch = "tickets"\n\n[jira]\nurl = "x"\nuser = "u"\nproject = "P"\n',
        encoding="utf-8",
    )
    cfg.write_jira_config(clear=True)
    data = tomllib.loads((p / "rebar.toml").read_text(encoding="utf-8"))
    assert "jira" not in data  # emptied table dropped
    assert data["tracker"]["branch"] == "tickets"  # other section kept


# ── the wizard: detect / persist / validate paths ──────────────────────────────
def _answers(monkeypatch, values):
    it = iter(values)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(it))


def test_detect_shows_existing_settings(tmp_path, monkeypatch, capsys) -> None:
    p = _proj(tmp_path, monkeypatch)
    (p / "rebar.toml").write_text(
        '[jira]\nurl = "https://have"\nuser = "have@x"\nproject = "HV"\n', encoding="utf-8"
    )
    cfg.reset_config_cache()
    # Non-interactive (flags) so no prompt; --no-validate to skip the probe.
    code = main(["jira-onboard", "--project", "HV", "--no-validate"])
    out = capsys.readouterr().out
    assert code == 0
    assert "https://have" in out and "have@x" in out  # detected values surfaced


def test_persist_via_prompts_writes_config(tmp_path, monkeypatch, capsys) -> None:
    _proj(tmp_path, monkeypatch)
    _answers(monkeypatch, ["https://prompted", "p@x", "PR"])
    code = main(["jira-onboard", "--no-validate"])
    out = capsys.readouterr().out
    assert code == 0
    cfg.reset_config_cache()
    jira = cfg.load_config().jira
    assert (jira.url, jira.user, jira.project) == ("https://prompted", "p@x", "PR")
    assert "JIRA_API_TOKEN" in out  # env-only-token guidance shown


def test_empty_then_eof_aborts_with_no_write(tmp_path, monkeypatch, capsys) -> None:
    p = _proj(tmp_path, monkeypatch)

    def _raise(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    code = main(["jira-onboard", "--no-validate"])
    assert code == 1
    assert not (p / "rebar.toml").exists()  # no partial write


def test_token_absent_skips_probe_exit_zero(tmp_path, monkeypatch, capsys) -> None:
    _proj(tmp_path, monkeypatch)
    # No JIRA_API_TOKEN in env → probe is skipped, exit 0, guidance printed.
    code = main(["jira-onboard", "--url", "https://x", "--user", "u", "--project", "P"])
    out = capsys.readouterr().out
    assert code == 0
    assert "bridge-probe" in out and "JIRA_API_TOKEN" in out


def test_validate_injects_resolved_settings_into_probe_env(tmp_path, monkeypatch, capsys) -> None:
    _proj(tmp_path, monkeypatch)
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    captured = {}

    def _fake_probe(argv, *, extra_env=None):
        captured["extra_env"] = extra_env
        return 0

    monkeypatch.setattr("rebar._cli._bridge_probe", _fake_probe)
    code = main(["jira-onboard", "--url", "https://j", "--user", "u@x", "--project", "PJ"])
    assert code == 0
    assert captured["extra_env"] == {
        "JIRA_URL": "https://j",
        "JIRA_USER": "u@x",
        "JIRA_PROJECT": "PJ",
    }


def test_no_validate_skips_probe_even_with_token(tmp_path, monkeypatch, capsys) -> None:
    _proj(tmp_path, monkeypatch)
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    def _fail(*a, **k):  # the probe must NOT be invoked
        raise AssertionError("bridge-probe should be skipped with --no-validate")

    monkeypatch.setattr("rebar._cli._bridge_probe", _fail)
    code = main(
        ["jira-onboard", "--url", "https://x", "--user", "u", "--project", "P", "--no-validate"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "--no-validate" in out or "bridge-probe" in out


def test_probe_failure_is_surfaced_and_config_kept(tmp_path, monkeypatch, capsys) -> None:
    _proj(tmp_path, monkeypatch)
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setattr("rebar._cli._bridge_probe", lambda argv, *, extra_env=None: 2)
    code = main(["jira-onboard", "--url", "https://x", "--user", "u", "--project", "P"])
    assert code == 2  # the probe's non-zero exit is propagated
    # ...but the config was still persisted (write and validate are separate steps).
    cfg.reset_config_cache()
    assert cfg.load_config().jira.url == "https://x"


def test_reset_clears_and_exits(tmp_path, monkeypatch, capsys) -> None:
    p = _proj(tmp_path, monkeypatch)
    (p / "rebar.toml").write_text(
        '[jira]\nurl = "x"\nuser = "u"\nproject = "P"\n', encoding="utf-8"
    )
    cfg.reset_config_cache()
    code = main(["jira-onboard", "--reset", "--yes"])
    assert code == 0
    data = tomllib.loads((p / "rebar.toml").read_text(encoding="utf-8"))
    assert "jira" not in data


def test_reset_declined_makes_no_change(tmp_path, monkeypatch) -> None:
    p = _proj(tmp_path, monkeypatch)
    before = '[jira]\nurl = "x"\n'
    (p / "rebar.toml").write_text(before, encoding="utf-8")
    _answers(monkeypatch, ["n"])
    code = main(["jira-onboard", "--reset"])
    assert code == 1
    assert (p / "rebar.toml").read_text(encoding="utf-8") == before
