"""A set-but-EMPTY boolean config value is a FAULT, not a silent False (bug b2ff).

``REBAR_MCP_READONLY=`` (the empty string) used to be an accepted spelling of ``False``
— ``_config_coercion._FALSE`` listed ``""`` — while ``_config_sources.env_overrides``
gates the env layer on key PRESENCE. So an env var expanded from an unset ``${VAR}`` in
a compose/systemd/k8s env block entered the layer stack as ``""``, outranked an explicit
``[mcp] readonly = true``, and brought the MCP server up with its full write surface
(``_mcp_writes`` registers no write tools only when ``ctx.readonly()``). Every OTHER
malformed value raised. The empty string was the one input that resolved SILENTLY, and
on ``readonly`` it resolved in the UNSAFE direction.

The fix treats a set-but-empty (or whitespace-only) boolean as a fault — a
:class:`ConfigError` — rather than as "unset". That is the only direction-neutral
answer, and it is what operator ruling 39f8-ae7c requires: *"a fault must error, never
silently resolve to a default, even a safe one."* Reading ``""`` as "unset" would
itself be a silent resolution, and not even a safe one: on the CAPABILITY-direction
gates (``mcp.allow_llm`` and friends, where ``False`` means the capability is withheld)
"unset" semantics would let a file's ``true`` through and turn a capability ON where the
current behaviour withholds it.

Genuinely-unset is untouched: ``env_overrides`` still keys on presence, so an absent var
contributes no layer at all and the deliberate "unset means denied" posture of the
capability gates is preserved. Only a PRESENT-but-empty value — the
``${VAR}``-expanded-to-nothing shape — is the fault.

These tests drive the REAL resolvers (``config.mcp_readonly`` / ``config.mcp_gate`` /
``load_config``) against a real on-disk config file, not a stub.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import config as cfg
from rebar._config_coercion import ConfigError, _as_bool

# Every boolean config key whose ``False`` means A PROTECTION IS OFF. On these, reading a
# set-but-empty env var as False silently WEAKENS the deployment — the bug's class.
_PROTECTION_DIRECTION_KEYS = (
    ("mcp", "readonly", "REBAR_MCP_READONLY"),
    ("mcp", "auth_enabled", "REBAR_MCP_AUTH_ENABLED"),
    ("identity", "require_authenticated", "REBAR_IDENTITY_REQUIRE_AUTHENTICATED"),
    ("verify", "require_plan_review_for_claim", "REBAR_VERIFY_REQUIRE_PLAN_REVIEW_FOR_CLAIM"),
    (
        "verify",
        "require_completion_verification_for_close",
        "REBAR_VERIFY_REQUIRE_COMPLETION_VERIFICATION_FOR_CLOSE",
    ),
    ("verify", "require_ticket_for_commit", "REBAR_VERIFY_REQUIRE_TICKET_FOR_COMMIT"),
    ("verify", "enforce_plan_material_pins", "REBAR_VERIFY_ENFORCE_PLAN_MATERIAL_PINS"),
)

# Boolean keys whose ``False`` means A CAPABILITY IS WITHHELD. An empty value was already
# "safe" here by luck of direction — but it was still silent, and it must not start
# resolving to the FILE's value either (that would turn a capability on).
_CAPABILITY_DIRECTION_KEYS = (
    ("mcp", "allow_llm", "REBAR_MCP_ALLOW_LLM"),
    ("mcp", "allow_jira_sync", "REBAR_MCP_ALLOW_JIRA_SYNC"),
)

_BOOL_ENV_NAMES = tuple(
    env for _, _, env in (*_PROTECTION_DIRECTION_KEYS, *_CAPABILITY_DIRECTION_KEYS)
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate from the host/CI env: any of these set would outrank the file these tests
    write (CI sets ``REBAR_MCP_ALLOW_LLM``). Requested explicitly rather than autouse —
    every test here either takes it directly or takes ``project``, which depends on it."""
    for name in ("REBAR_CONFIG", "XDG_CONFIG_HOME", "REBAR_ROOT", *_BOOL_ENV_NAMES):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None) -> Path:
    """A real repo root whose ``rebar.toml`` turns every gate under test ON, so an env
    layer that resolves to False is visibly DEFEATING the file rather than agreeing with
    a default."""
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / "rebar.toml").write_text(
        "[mcp]\n"
        "readonly = true\n"
        "allow_llm = true\n"
        "allow_jira_sync = true\n"
        "auth_enabled = true\n"
        "\n"
        "[identity]\n"
        "require_authenticated = true\n"
        "\n"
        "[verify]\n"
        "require_plan_review_for_claim = true\n"
        "require_completion_verification_for_close = true\n"
        "require_ticket_for_commit = true\n"
        "enforce_plan_material_pins = true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    cfg.reset_config_cache()
    return root


def _resolve(section: str, key: str) -> bool:
    return bool(getattr(getattr(cfg.load_config(), section), key))


def _set(monkeypatch: pytest.MonkeyPatch, env: str, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(env, raising=False)
    else:
        monkeypatch.setenv(env, value)
    cfg.reset_config_cache()


# ── the reported defect, on the reported key, through the owned resolver ────────


@pytest.mark.parametrize("empty", ["", "  ", "\t", "\n"])
def test_empty_env_does_not_silently_disable_the_readonly_gate(
    project: Path, monkeypatch: pytest.MonkeyPatch, empty: str
) -> None:
    """The bug: this returned ``False`` — the write surface came up, silently, against
    an explicit ``readonly = true``."""
    _set(monkeypatch, "REBAR_MCP_READONLY", empty)
    with pytest.raises(ConfigError) as excinfo:
        cfg.mcp_readonly()
    message = str(excinfo.value)
    assert "read-only" in message, f"the error does not name the gate: {message!r}"
    assert "39f8-ae7c" in message, f"the error does not cite the ruling: {message!r}"


def test_unset_env_leaves_the_file_in_charge(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Genuinely-unset is NOT the fault and must keep working: the env layer
    contributes nothing and ``readonly = true`` is honoured."""
    _set(monkeypatch, "REBAR_MCP_READONLY", None)
    assert cfg.mcp_readonly() is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("false", False), ("0", False), ("off", False), ("true", True), ("1", True), ("ON", True)],
)
def test_explicit_env_values_still_override_the_file(
    project: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    """An operator who MEANS to turn the gate off still can — the fix narrows only the
    empty string, not the accepted vocabulary."""
    _set(monkeypatch, "REBAR_MCP_READONLY", value)
    assert cfg.mcp_readonly() is expected


def test_malformed_env_still_raises(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the pre-existing loud behaviour the empty string was the sole exception to."""
    _set(monkeypatch, "REBAR_MCP_READONLY", "garbage")
    with pytest.raises(ConfigError):
        cfg.mcp_readonly()


# ── AC2: the whole PROTECTION-direction class, not just `readonly` ──────────────


@pytest.mark.parametrize(("section", "key", "env"), _PROTECTION_DIRECTION_KEYS)
def test_empty_env_never_silently_turns_a_protection_off(
    project: Path, monkeypatch: pytest.MonkeyPatch, section: str, key: str, env: str
) -> None:
    _set(monkeypatch, env, "")
    with pytest.raises(ConfigError):
        _resolve(section, key)


@pytest.mark.parametrize(("section", "key", "env"), _PROTECTION_DIRECTION_KEYS)
def test_unset_env_honours_the_file_for_every_protection_gate(
    project: Path, monkeypatch: pytest.MonkeyPatch, section: str, key: str, env: str
) -> None:
    _set(monkeypatch, env, None)
    assert _resolve(section, key) is True


# ── AC3: the capability gates keep their "unset means denied" posture ───────────


@pytest.mark.parametrize(("section", "key", "env"), _CAPABILITY_DIRECTION_KEYS)
def test_capability_gate_denies_when_absent_from_both_env_and_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
    section: str,
    key: str,
    env: str,
) -> None:
    """With no env var AND no file key, the built-in default withholds the capability.
    This is the posture the fix must not disturb, so it is asserted against a config
    that is SILENT on the key rather than one that sets it."""
    root = tmp_path / "bare"
    (root / ".git").mkdir(parents=True)
    (root / "rebar.toml").write_text("[ticket]\ndisplay_mode = 'auto'\n", encoding="utf-8")
    monkeypatch.chdir(root)
    _set(monkeypatch, env, None)
    assert _resolve(section, key) is False


@pytest.mark.parametrize(("section", "key", "env"), _CAPABILITY_DIRECTION_KEYS)
def test_capability_gate_empty_env_is_a_fault_not_a_quiet_grant(
    project: Path, monkeypatch: pytest.MonkeyPatch, section: str, key: str, env: str
) -> None:
    """The empty string must NOT be re-read as "unset" here: the file says ``true``, so
    unset-semantics would GRANT the capability where today it is withheld. Erroring is
    the direction-neutral answer."""
    _set(monkeypatch, env, "")
    with pytest.raises(ConfigError):
        _resolve(section, key)


@pytest.mark.parametrize(("section", "key", "env"), _CAPABILITY_DIRECTION_KEYS)
def test_capability_gate_explicit_false_still_withholds(
    project: Path, monkeypatch: pytest.MonkeyPatch, section: str, key: str, env: str
) -> None:
    _set(monkeypatch, env, "false")
    assert _resolve(section, key) is False


# ── the coercion seam itself, so the class rule is pinned at its definition ─────


@pytest.mark.parametrize("value", ["", " ", "\t\n"])
def test_as_bool_rejects_blank_strings(value: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        _as_bool(value, "some.key")
    message = str(excinfo.value)
    assert "some.key" in message, f"the error does not name the key: {message!r}"
    assert "empty" in message.lower(), (
        f"the error does not explain the EMPTY case, which reads as a mystery "
        f"otherwise (a bare `got ''`): {message!r}"
    )


@pytest.mark.parametrize("value", ["true", "1", "yes", "on", " TRUE "])
def test_as_bool_still_accepts_true_spellings(value: str) -> None:
    assert _as_bool(value, "some.key") is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", " FALSE "])
def test_as_bool_still_accepts_false_spellings(value: str) -> None:
    assert _as_bool(value, "some.key") is False


def test_as_bool_passes_through_real_booleans() -> None:
    """TOML ``key = true`` arrives as a real ``bool`` and must not go near the string
    vocabulary."""
    assert _as_bool(True, "some.key") is True
    assert _as_bool(False, "some.key") is False
