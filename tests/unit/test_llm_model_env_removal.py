"""`REBAR_LLM_MODEL` was REMOVED and TOMBSTONED (pre-1.0 breaking pass #3, ticket 6cc4).

History: story d23e deprecated the bare variable once model CLASSES became the interface
(ADR 0057) -- a SUPERSESSION, not a rename, so it was `_scheduled(...)` rather than
`_permanent(...)`. It shipped deprecated in v0.11.0 and was removed early by operator ruling,
the same lever DE7 and the ticket-5899 pass used.

WHY A TOMBSTONE, AND WHY `error`. The tombstone registry covers `env`/`cfg`/`file` inputs --
things an operator can still have SET after the code stopped reading them. Silently ignoring a
still-set `REBAR_LLM_MODEL` would quietly change which model every operation runs, i.e. cost and
quality, with no signal at all. So it fails LOUD, matching `REBAR_LLM_MAX_ITERS`, the closest
precedent (also a superseded LLM knob, also enforced in `LLMConfig.from_env`).

WHERE THE CHECK LIVES. In `LLMConfig.from_env`, NOT the core config layer, so a retired LLM knob
fails only when the LLM stack actually loads. `RemovedInputError` subclasses `BaseException` so
the broad `except Exception` on this method's tracker-probe path cannot demote it to a silent
default.

WHAT SURVIVES. The CONFIG key `[tool.rebar.llm].model` is NOT removed -- it is still the
top-level model knob, resolving CLI > config table > `DEFAULT_MODEL` with no env channel. The
per-class `REBAR_LLM_<CLASS>_MODEL` variables and the per-step workflow `model:` override are
likewise untouched.
"""

from __future__ import annotations

import pytest

from rebar._deprecations import REGISTRY, RemovedInputError, tombstone_for
from rebar.llm.config import DEFAULT_MODEL, LLMConfig

_VAR = "REBAR_LLM_MODEL"


# ── the registry state: gone from the alias registry, present as a tombstone ──
def test_the_deprecation_row_is_gone_from_the_alias_registry() -> None:
    """The alias registry HONORS old surfaces; a removed one must not linger there."""
    assert f"env:{_VAR}" not in REGISTRY, (
        f"env:{_VAR} was removed -- it must not remain in the honoring alias registry"
    )


def test_the_variable_is_tombstoned_as_a_fail_loud_env_input() -> None:
    ri = tombstone_for("env", _VAR)
    assert ri is not None, f"{_VAR} must be tombstoned so a still-set value is not ignored"
    assert ri.behavior == "error", (
        "silently ignoring a still-set model id would change which model every operation "
        "runs -- it must fail loud, like REBAR_LLM_MAX_ITERS"
    )
    assert "model_classes" in ri.replacement, "the message must name the replacement interface"


# ── runtime: a still-set value fails loud rather than being ignored ────────────
def test_a_still_set_variable_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_VAR, "anthropic:claude-opus-4-8")
    with pytest.raises(RemovedInputError) as exc:
        LLMConfig.from_env()
    msg = str(exc.value)
    assert _VAR in msg and "removed" in msg.lower(), msg
    assert "model_classes" in msg, "the error must point at the replacement"


def test_the_error_is_a_baseexception_so_no_broad_handler_swallows_it() -> None:
    """The config -> tracker -> MCP path is riddled with broad `except Exception`; a retired
    input must sail through all of them rather than be demoted to a silent default."""
    assert issubclass(RemovedInputError, BaseException)
    assert not issubclass(RemovedInputError, Exception)


# ── what survives: the config key, the CLI override, the per-class variables ──
def test_the_cli_override_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """`rebar -c llm.model=...` is the CLI rung; it survives the env channel's removal.
    There is no `from_env(cli=...)` kwarg -- `from_env` reads `cli_overrides_for("llm")`."""
    from rebar import config as root_config

    monkeypatch.delenv(_VAR, raising=False)
    previous = root_config.cli_overrides_for("llm")
    root_config.set_cli_overrides(
        root_config.parse_cli_overrides(["llm.model=anthropic:claude-test-cli"])
    )
    try:
        assert LLMConfig.from_env().model == "anthropic:claude-test-cli"
    finally:
        root_config.set_cli_overrides({"llm": previous} if previous else {})


def test_the_default_applies_with_no_env_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_VAR, raising=False)
    assert LLMConfig.from_env().model == DEFAULT_MODEL


def test_the_per_class_variables_are_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar.llm.model_classes import load_class_slots

    monkeypatch.delenv(_VAR, raising=False)
    monkeypatch.setenv("REBAR_LLM_FRONTIER_MODEL", "openai:gpt-4o")
    slots = load_class_slots(None)
    assert slots["frontier"].model == "openai:gpt-4o"


def test_no_fan_out_rung_remains(monkeypatch: pytest.MonkeyPatch) -> None:
    """The removed variable used to sit at the DEFAULT position and fan ONE value out to all
    three classes. With it gone, class slots fall through to their BUILT-IN defaults."""
    from rebar.llm.model_classes import _DEFAULT_MODEL_BY_CLASS, load_class_slots

    monkeypatch.delenv(_VAR, raising=False)
    for name in ("frontier", "standard", "trivial"):
        monkeypatch.delenv(f"REBAR_LLM_{name.upper()}_MODEL", raising=False)
    slots = load_class_slots(None)
    for name, slot in slots.items():
        assert slot.model == _DEFAULT_MODEL_BY_CLASS[name]


# ── docs: no prose may still advertise the variable as a live knob ────────────
def test_no_prose_doc_advertises_the_variable_as_live() -> None:
    """The inverse of the old deprecation-marking guard: prose may mention the variable only
    as REMOVED history, never as something an operator can set today."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for rel in (
        "docs/config.md",
        "docs/llm-framework.md",
        "docs/llm-example-configs.md",
        "docs/local-dev-env.md",
        "docs/ci-provider-matrix.md",
        "docs/plan-review-gate.md",
        "docs/workflow-authoring-v2.md",
        "infra/runbooks/review-bot-ops.md",
        "infra/compose/docker-compose.yml",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        for line in text.splitlines():
            if not re.search(rf"{_VAR}\b(?!_)", line):
                continue
            if re.search(r"remov|tombston|fail loud|would have|used to|since been", line, re.I):
                continue
            # A mention whose removal context sits on a neighbouring line is fine; require the
            # paragraph to carry it.
            offenders.append(f"{rel}: {line.strip()[:110]}")
    # Every surviving mention must sit in a paragraph that marks the variable removed.
    for rel_line in offenders:
        rel = rel_line.split(":", 1)[0]
        text = (root / rel).read_text(encoding="utf-8")
        assert re.search(r"remov|tombston", text, re.I), (
            f"{rel} mentions {_VAR} but never says it was removed: {rel_line}"
        )
