"""`REBAR_LLM_MODEL` becomes a deprecated shorthand that fans out to all three classes (d23e).

Once model CLASSES are the interface, a single bare `REBAR_LLM_MODEL` is a second, ambiguous way to
say the same thing. It is DEPRECATED WITH A MIGRATION WINDOW -- `_scheduled(...)`, not
`_permanent(...)`. That distinction took two wrong turns to settle, so the reason is recorded here
rather than left to the next reader:

  `_deprecations.py:71-73` says the six existing env entries are `_permanent` because they are
  "stable REBAR_-prefixed renames of established names" -- the SAME knob under a better name, so
  removal would be pointless. `REBAR_LLM_MODEL` is not a rename; it is SUPERSEDED by a different
  interface (per-class slots). That is what `_scheduled` exists for. The six precedents are
  precedent for RENAMES, NOT for supersessions, and parent eb58 decided the window explicitly.

WHERE THE WARNING LIVES, and why it is TWO call sites rather than one. The warning fires AT EVERY
ENV READ and nowhere else. There are exactly two reads:

  * `config.py:469` -- `_llm_str(table, cli, "REBAR_LLM_MODEL", "model", DEFAULT_MODEL)`;
  * `model_classes.py` -- the new read that the fan-out needs.

It must NOT be placed in `resolve_model_string`, even though every path reaches it. `resolve_model`
calls `resolve_model_string(step or workflow or cfg.model)` -- an ALREADY-RESOLVED string, where the
value is indistinguishable from a config-table `model` key or a per-step `model:`. A warning there
would fire at operators who never set the variable. PROVENANCE EXISTS ONLY AT THE ENV READ; the
negative control below is what forbids that placement.

EMISSION IS PER CALL. `warn_deprecated` has no dedup -- no `_seen` set, no `_emitted` flag, no
`warnings.warn` filter -- and none of the six existing env deprecations dedupes either. An earlier
draft of this plan required "once per process"; that was invented and is withdrawn. So the
multiplicity assertions here are PER-CALL counts, and a second call must warn again.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPRECATIONS = REPO_ROOT / "src" / "rebar" / "_deprecations.py"
_CONFIG = REPO_ROOT / "src" / "rebar" / "llm" / "config.py"
_MODEL_CLASSES = REPO_ROOT / "src" / "rebar" / "llm" / "model_classes.py"
_LLM_COMMANDS = REPO_ROOT / "src" / "rebar" / "_cli" / "_llm_commands.py"
_MCP_LLM = REPO_ROOT / "src" / "rebar" / "_mcp_llm.py"

_KEY = "env:REBAR_LLM_MODEL"


def _warnings(caplog) -> list[str]:
    """Every emitted deprecation message naming REBAR_LLM_MODEL.

    `warn_deprecated`'s default channel is `via="log"` -> `logger.warning(msg)`, so log capture is
    the right probe, not `pytest.warns`.
    """
    return [r.getMessage() for r in caplog.records if "REBAR_LLM_MODEL" in r.getMessage()]


@pytest.fixture
def _no_model_env(monkeypatch):
    """A clean slate. The autouse `_no_ambient_model_classes` fixture scrubs the nine per-class
    vars but NOT `REBAR_LLM_MODEL`, which is the variable under test here."""
    monkeypatch.delenv("REBAR_LLM_MODEL", raising=False)
    return monkeypatch


# ══ HAPPY PATH ════════════════════════════════════════════════════════════════════════


def test_the_deprecation_is_registered_as_scheduled_not_permanent() -> None:
    """The REGISTRATION itself, which no criterion covered until late: a `_permanent` entry would
    still emit a warning and still pass every emission test, while silently promising the opposite
    lifecycle -- "no removal planned" instead of a migration window."""
    from rebar._deprecations import REGISTRY

    assert _KEY in REGISTRY, f"{_KEY} is not registered; warn_deprecated will raise KeyError"
    dep = REGISTRY[_KEY]
    assert dep.permanent is False, "must be _scheduled(...) -- a SUPERSESSION, not a rename"
    assert dep.remove_in, "a scheduled deprecation needs a removal milestone"
    assert "model_classes" in dep.replacement or "class" in dep.replacement.lower(), (
        f"replacement should point at the class interface, got {dep.replacement!r}"
    )


def test_a_bare_model_env_var_fans_out_to_all_three_classes(_no_model_env) -> None:
    """The core behaviour: one knob, three slots. Widest-compatibility reading -- an operator who
    set the old variable gets it honoured everywhere, not just for one class."""
    _no_model_env.setenv("REBAR_LLM_MODEL", "anthropic:claude-test-fanout")
    from rebar.llm.model_classes import CLASS_NAMES, load_class_slots

    slots = load_class_slots()
    assert set(slots) == set(CLASS_NAMES)
    for name in CLASS_NAMES:
        assert slots[name].model == "anthropic:claude-test-fanout", (
            f"class {name!r} did not pick up the bare REBAR_LLM_MODEL"
        )


def test_the_config_env_read_warns(_no_model_env, caplog) -> None:
    """PATH (a) of two. `config.from_env()` reaches `config.py:469` without necessarily invoking the
    fan-out, so this path needs its own warning and its own test."""
    _no_model_env.setenv("REBAR_LLM_MODEL", "anthropic:claude-test-cfg")
    from rebar.llm.config import LLMConfig

    with caplog.at_level(logging.WARNING):
        LLMConfig.from_env()
    assert _warnings(caplog), "config.from_env() did not emit the deprecation warning"


def test_the_model_classes_env_read_warns(_no_model_env, caplog) -> None:
    """PATH (b) of two, and the one an earlier draft would have missed entirely.
    `load_class_slots()` is reachable WITHOUT `config.from_env()`, so a config-only warning leaves
    this path fanning the variable out to all three classes SILENTLY."""
    _no_model_env.setenv("REBAR_LLM_MODEL", "anthropic:claude-test-mc")
    from rebar.llm.model_classes import load_class_slots

    with caplog.at_level(logging.WARNING):
        load_class_slots()
    assert _warnings(caplog), "load_class_slots() did not emit the deprecation warning"


def test_explicit_class_config_beats_the_deprecated_var(_no_model_env, caplog) -> None:
    """Precedence: the new interface wins over the old shorthand, and the warning still fires --
    the operator still has a stale export to clean up even though it lost."""
    _no_model_env.setenv("REBAR_LLM_MODEL", "anthropic:claude-test-loser")
    _no_model_env.setenv("REBAR_LLM_STANDARD_MODEL", "anthropic:claude-test-winner")
    from rebar.llm.model_classes import load_class_slots

    with caplog.at_level(logging.WARNING):
        slots = load_class_slots()
    assert slots["standard"].model == "anthropic:claude-test-winner"
    assert slots["frontier"].model == "anthropic:claude-test-loser", (
        "the un-overridden classes must still receive the fan-out"
    )
    assert _warnings(caplog), "the warning must still fire even when a class override wins"


# ══ HELD OUT ══════════════════════════════════════════════════════════════════════════


def test_the_negative_control_no_warning_without_the_env_var(_no_model_env, caplog) -> None:
    """THE CRITERION THAT FORBIDS PLACING THE WARNING IN `resolve_model_string`.

    `resolve_model` passes an already-resolved string, so a warning there cannot tell a
    config-table `model` key from the env var and would fire at operators who never set it. With
    the variable UNSET, resolving an ordinary model string must stay silent.
    """
    from rebar.llm.model_classes import resolve_model_string

    with caplog.at_level(logging.WARNING):
        resolve_model_string("anthropic:claude-opus-4-8")
        load_ok = resolve_model_string("standard")
    assert _warnings(caplog) == [], (
        "a deprecation warning fired with REBAR_LLM_MODEL unset -- the warning is on the resolved "
        f"string rather than at the env read (got: {_warnings(caplog)}, resolved: {load_ok!r})"
    )


def test_cli_precedence_the_var_still_warns_when_overridden(_no_model_env, caplog) -> None:
    """WARN-WHEN-SET, pinned. `_llm_str` is CLI > env > file > default and RETURNS EARLY on a CLI
    hit, so `os.environ.get` is never reached when `--model` wins. Two semantics are therefore
    possible and the suite cannot otherwise tell them apart: warn-when-SET vs warn-when-env-WINS.
    Warn-when-set is chosen -- an exported deprecated variable is still a migration item -- and
    warn-when-env-wins was rejected because it cannot live inside `_llm_str` (shared by ~a dozen
    variables) without duplicating the precedence logic at the call site.
    """
    _no_model_env.setenv("REBAR_LLM_MODEL", "anthropic:claude-test-env")
    from rebar import config as root_config
    from rebar.llm.config import LLMConfig

    # The real CLI layer, driven the way `tests/unit/test_config_llm.py:126` drives it -- there is
    # no `from_env(cli=...)` kwarg; `from_env` reads `cli_overrides_for("llm")` itself.
    previous = root_config.cli_overrides_for("llm")
    root_config.set_cli_overrides(
        root_config.parse_cli_overrides(["llm.model=anthropic:claude-test-cli"])
    )
    try:
        with caplog.at_level(logging.WARNING):
            cfg = LLMConfig.from_env()
    finally:
        root_config.set_cli_overrides({"llm": previous} if previous else {})
    assert cfg.model == "anthropic:claude-test-cli", "CLI must still win precedence"
    assert _warnings(caplog), (
        "warn-when-SET chosen: the warning must fire even when --model overrides the env var"
    )


def test_one_call_emits_exactly_one_warning_not_one_per_class(_no_model_env, caplog) -> None:
    """MULTIPLICITY, and the reason the read is hoisted. `_parse_slot` runs ONCE PER CLASS, so a
    read-and-warn placed inside it emits THREE identical warnings for one `load_class_slots()`
    call. Legal under per-call emission, but bad output -- so the read is hoisted into
    `parse_class_slots`, which loops the classes, and the default is passed down.

    This is a PER-CALL count, NOT once-per-process: see the sibling test below.
    """
    _no_model_env.setenv("REBAR_LLM_MODEL", "anthropic:claude-test-count")
    from rebar.llm.model_classes import load_class_slots

    with caplog.at_level(logging.WARNING):
        load_class_slots()
    assert len(_warnings(caplog)) == 1, (
        f"expected exactly 1 warning per load_class_slots() call, got {len(_warnings(caplog))} -- "
        "the read is inside _parse_slot (once per class) instead of hoisted into parse_class_slots"
    )


def test_emission_is_per_call_a_second_call_warns_again(_no_model_env, caplog) -> None:
    """THE GUARD AGAINST RE-INTRODUCING THE WITHDRAWN REQUIREMENT. An earlier draft demanded
    "once per process". `warn_deprecated` provides no such thing and none of the six existing env
    deprecations dedupes, so adding per-process state here would make this surface the odd one out.
    Two calls must produce two warnings."""
    _no_model_env.setenv("REBAR_LLM_MODEL", "anthropic:claude-test-twice")
    from rebar.llm.model_classes import load_class_slots

    with caplog.at_level(logging.WARNING):
        load_class_slots()
        load_class_slots()
    assert len(_warnings(caplog)) == 2, (
        f"expected 2 warnings from 2 calls (per-call emission), got {len(_warnings(caplog))} -- "
        "per-process dedup was withdrawn and must not be reintroduced"
    )


def test_neither_set_leaves_defaults_untouched_and_silent(_no_model_env, caplog) -> None:
    """The no-op control. Most operators set nothing; they must see no behaviour change and no
    warning. Without this, a fan-out that accidentally applied its default unconditionally would
    look correct in every other test here."""
    from rebar.llm.model_classes import _DEFAULT_MODEL_BY_CLASS, load_class_slots

    with caplog.at_level(logging.WARNING):
        slots = load_class_slots()
    for name, expected in _DEFAULT_MODEL_BY_CLASS.items():
        assert slots[name].model == expected, f"class {name!r} default changed"
    assert _warnings(caplog) == [], "warned with REBAR_LLM_MODEL unset"


def test_the_registry_comment_no_longer_claims_permanent_only() -> None:
    """Scope mandates this in bold, so it gets a criterion: adding a `_scheduled` entry makes the
    `:75-79` block comment FALSE. It asserted the registry "now holds ONLY these permanent renames:
    every remaining SCHEDULED (removable) surface has been removed in the pre-1.0 breaking passes."
    Without this test an executor could close every other AC and ship a comment the code
    contradicts."""
    src = _DEPRECATIONS.read_text(encoding="utf-8")
    assert "holds ONLY these permanent" not in src.replace("\n", " ").replace("  ", " "), (
        "the block comment still claims the registry holds only permanent renames"
    )
    assert re.search(r"supersession|superseded", src, re.I), (
        "the corrected comment should say why this entry is scheduled -- a supersession"
    )


def test_the_rendered_operator_facing_strings_no_longer_advertise_the_var() -> None:
    """NOT comments -- these RENDER. `_llm_commands.py:160` is the `argparse` `description=` for
    `rebar review`, i.e. `--help` text; `_mcp_llm.py:64` is an MCP tool description surfaced to
    clients; `config.py:10` is the module docstring's env table presenting the variable as THE
    model knob. An operator reading any of them is sent to a deprecated interface."""
    offenders = []
    for path in (_LLM_COMMANDS, _MCP_LLM):
        text = path.read_text(encoding="utf-8")
        if "provider per REBAR_LLM_MODEL" in text:
            offenders.append(f"{path.relative_to(REPO_ROOT)} still advertises it as the interface")
    cfg = _CONFIG.read_text(encoding="utf-8")
    if re.search(r"REBAR_LLM_MODEL\s+model id \(default", cfg):
        offenders.append("llm/config.py:10 still presents it as 'model id (default ...)'")
    assert offenders == [], "; ".join(offenders)


def test_the_four_example_configs_exist_and_are_complete_tables() -> None:
    """Four NAMED configurations, each a whole paste-able table rather than a fragment. Without
    the completeness check this criterion could be satisfied by prose ABOUT configs."""
    doc = REPO_ROOT / "docs" / "llm-example-configs.md"
    assert doc.exists(), "docs/llm-example-configs.md was not created"
    text = doc.read_text(encoding="utf-8")
    for label in ("Anthropic only", "Bedrock only", "Mixed provider", "Local model"):
        assert label.lower() in text.lower(), f"example config missing: {label}"
    # Each config must configure all three classes, i.e. be usable as-is.
    assert text.count("model_classes") >= 4, "each example must carry its own model_classes table"
    for cls in ("frontier", "standard", "trivial"):
        assert text.count(cls) >= 4, f"{cls} is not configured in all four examples"


def test_every_prose_doc_marks_the_variable_deprecated() -> None:
    """AC: no doc may still present `REBAR_LLM_MODEL` as THE way to choose a model. Each file
    mentions it must also mark it deprecated -- checked per file, because a repo-wide grep for the
    word "deprecated" would pass on one file's note while another still advertises it.

    `REBAR_LLM_MODEL_PROVIDER` is a DIFFERENT variable and is NOT deprecated, so the match is
    anchored to the exact name followed by a non-identifier character.
    """
    bare = re.compile(r"REBAR_LLM_MODEL(?![A-Z_])")
    stale = []
    for rel in (
        "README.md",
        "docs/llm-framework.md",
        "docs/config.md",
        "docs/plan-review-gate.md",
        "docs/workflow-authoring-v2.md",
    ):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        if bare.search(text) and not re.search(r"deprecated|DEPRECATED", text):
            stale.append(rel)
    assert stale == [], f"these mention the bare variable without marking it deprecated: {stale}"


def test_the_adr_records_the_decision_and_the_rejected_alternatives() -> None:
    """The ADR must record WHY a window rather than a permanent alias -- the question that was
    decided wrongly twice. An ADR that only states the outcome would let it be re-litigated."""
    adrs = list((REPO_ROOT / "docs" / "adr").glob("*model-classes*.md"))
    assert adrs, "no ADR recording the model-class model and the deprecation"
    text = adrs[0].read_text(encoding="utf-8")
    assert re.search(r"rejected|Alternatives", text, re.I), (
        "the ADR records no rejected alternatives"
    )
    assert re.search(r"rename", text, re.I), (
        "the ADR must explain the rename-vs-supersession distinction that decides "
        "_scheduled over _permanent"
    )


def test_the_env_read_is_a_string_literal_so_the_docs_generator_sees_it() -> None:
    """`scripts/gen_env_registry.py` resolves an env name only when it is a STRING LITERAL
    (`gen_env_registry.py:117-124`). A read built dynamically would vanish from `docs/env-vars.md`
    while the CI drift gate stayed green -- exactly how bug b00f lost four variables."""
    src = _MODEL_CLASSES.read_text(encoding="utf-8")
    assert 'os.environ.get("REBAR_LLM_MODEL")' in src, (
        'model_classes.py must read the variable via a literal os.environ.get("REBAR_LLM_MODEL") '
        "so the env-registry generator can resolve it"
    )
