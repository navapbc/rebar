"""``REBAR_LLM_CONFIG_FILE``: an env-named TOML file that LAYERS over the discovered
LLM config (task cb6f).

``KUBECONFIG`` / ``AWS_CONFIG_FILE`` / ``DOCKER_CONFIG`` all REPLACE the discovered file;
rebar's pointer layers per key instead, because an environment that must restate the whole
config to change one key is a likelier source of drift than precedence subtlety. These pin
that layering, its merge semantics (tables deep-merge, arrays replace wholesale), its place
in the precedence chain (CLI > env > pointer > discovered > default), and the fail-loud
behaviour of a pointer naming a file that is not there.

Assertions are on OBSERVABLE behaviour — resolved config values, resolved model strings,
raised error types and messages — never on internal structure.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rebar import config as cfg
from rebar.llm.config import LLMConfig

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REBAR_CONFIG",
        "REBAR_LLM_CONFIG_FILE",
        "XDG_CONFIG_HOME",
        "REBAR_ROOT",
        "REBAR_LLM_MODEL",
        "REBAR_LLM_MAX_STEPS",
        "REBAR_LLM_TIMEOUT",
        "REBAR_LLM_FRONTIER_MODEL",
        "REBAR_LLM_FRONTIER_PROVIDER",
        "REBAR_LLM_FRONTIER_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg.set_cli_overrides(None)


def _proj(tmp: Path, body: str = "") -> Path:
    """A repo root whose DISCOVERED config is ``rebar.toml`` carrying ``body``."""
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    if body:
        (p / "rebar.toml").write_text(body, encoding="utf-8")
    return p


def _pointer(tmp: Path, body: str, monkeypatch: pytest.MonkeyPatch, name: str = "ci.toml") -> Path:
    """Write ``body`` to a file OUTSIDE the repo and point REBAR_LLM_CONFIG_FILE at it."""
    path = tmp / name
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv("REBAR_LLM_CONFIG_FILE", str(path))
    return path


def _cfg(root: Path) -> LLMConfig:
    cfg.reset_config_cache()
    return LLMConfig.from_env(repo_root=root)


def _slots(root: Path):
    from rebar.llm import model_classes

    cfg.reset_config_cache()
    return model_classes, model_classes.load_class_slots(root)


# ── the pointer is consumed ───────────────────────────────────────────────────────────
def test_a_key_set_only_in_the_pointed_file_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base case: a CI environment supplies a key the checkout's config never mentions."""
    p = _proj(tmp_path)
    _pointer(tmp_path, "[llm]\nmodel = 'pointed-model'\n", monkeypatch)
    assert _cfg(p).model == "pointed-model"


def test_the_pointed_file_wins_per_key_over_the_discovered_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precedence: pointer beats the discovered file for a key BOTH set."""
    p = _proj(tmp_path, "[llm]\nmodel = 'discovered-model'\n")
    _pointer(tmp_path, "[llm]\nmodel = 'pointed-model'\n", monkeypatch)
    assert _cfg(p).model == "pointed-model"


def test_a_key_only_in_the_discovered_config_survives_the_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE layering criterion — what distinguishes layering from replace. A pointer that
    overrides `model` must not silently discard the project's `max_steps`; a REPLACE
    implementation reverts it to the built-in default and this fails."""
    p = _proj(tmp_path, "[llm]\nmodel = 'discovered-model'\nmax_steps = 7\n")
    _pointer(tmp_path, "[llm]\nmodel = 'pointed-model'\n", monkeypatch)
    o = _cfg(p)
    assert o.model == "pointed-model"
    assert o.max_iterations == 7


def test_env_still_beats_the_pointed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pointer sits BELOW the per-key env layer: CLI > env > pointer > discovered."""
    p = _proj(tmp_path, "[llm]\nmodel = 'discovered-model'\n")
    _pointer(tmp_path, "[llm]\nmodel = 'pointed-model'\n", monkeypatch)
    monkeypatch.setenv("REBAR_LLM_MODEL", "env-model")
    assert _cfg(p).model == "env-model"


def test_cli_override_still_beats_the_pointed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Top of the chain: `rebar -c llm.model=…` outranks the pointer too."""
    p = _proj(tmp_path)
    _pointer(tmp_path, "[llm]\nmodel = 'pointed-model'\n", monkeypatch)
    cfg.reset_config_cache()  # clears the process-wide CLI overrides, so set them AFTER
    cfg.set_cli_overrides({"llm": {"model": "cli-model"}})
    assert LLMConfig.from_env(repo_root=p).model == "cli-model"


def test_an_empty_pointed_file_leaves_the_discovered_config_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hermetic-test affordance (`AWS_CONFIG_FILE=/dev/null`-style): pointing at a file
    with no `[llm]` table must be a clean no-op, never an error."""
    p = _proj(tmp_path, "[llm]\nmodel = 'discovered-model'\n")
    _pointer(tmp_path, "", monkeypatch)
    assert _cfg(p).model == "discovered-model"


# ── fail loud ─────────────────────────────────────────────────────────────────────────
def test_a_pointer_to_a_missing_path_raises_naming_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd pointer must NOT silently fall back to the discovered config — an operator
    who asked for a specific file and got the project's would debug the wrong thing. The
    error is a typed rebar config error and names the variable so the fix is obvious."""
    p = _proj(tmp_path, "[llm]\nmodel = 'discovered-model'\n")
    monkeypatch.setenv("REBAR_LLM_CONFIG_FILE", str(tmp_path / "nope.toml"))
    with pytest.raises(cfg.ConfigError) as excinfo:
        _cfg(p)
    assert "REBAR_LLM_CONFIG_FILE" in str(excinfo.value)


# ── merge semantics: tables deep-merge, arrays replace ────────────────────────────────
def test_nested_tables_deep_merge_preserving_every_discovered_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The criterion `dict.update` cannot satisfy. A pointer setting ONLY
    `model_classes.frontier.model` must keep the discovered `provider` AND the discovered
    `fallback` chain; a shallow merge replaces the whole `frontier` table and drops both."""
    p = _proj(
        tmp_path,
        "[llm.model_classes.frontier]\n"
        "model = 'discovered-model'\n"
        "provider = 'bedrock'\n"
        "fallback = [ { model = 'fb-one', provider = 'anthropic' },"
        " { model = 'fb-two', provider = 'anthropic' } ]\n",
    )
    _pointer(tmp_path, "[llm.model_classes.frontier]\nmodel = 'pointed-model'\n", monkeypatch)
    mc, slots = _slots(p)
    assert mc.resolve_class("frontier", slots) == "bedrock:pointed-model"
    assert mc.resolve_fallback_chain("frontier", slots) == [
        "anthropic:fb-one",
        "anthropic:fb-two",
    ]


def test_an_array_in_the_pointed_file_replaces_rather_than_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arrays are replaced wholesale. An appending merge would leave the project's chain
    still reachable behind CI's, so CI could never actually narrow the fallback set."""
    p = _proj(
        tmp_path,
        "[llm.model_classes.frontier]\n"
        "model = 'discovered-model'\n"
        "fallback = [ { model = 'fb-one', provider = 'anthropic' },"
        " { model = 'fb-two', provider = 'anthropic' } ]\n",
    )
    _pointer(
        tmp_path,
        "[llm.model_classes.frontier]\n"
        "fallback = [ { model = 'ci-only', provider = 'anthropic' } ]\n",
        monkeypatch,
    )
    mc, slots = _slots(p)
    assert mc.resolve_fallback_chain("frontier", slots) == ["anthropic:ci-only"]
    # the sibling `model` the pointer did NOT set still deep-merges through
    assert mc.resolve_class("frontier", slots).endswith("discovered-model")


def test_an_empty_array_in_the_pointed_file_clears_the_discovered_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fallback = []` is the "CI pins fail-loudly over a project default" case: an empty
    array must mean NO fallbacks, not "unset, keep the project's"."""
    p = _proj(
        tmp_path,
        "[llm.model_classes.frontier]\n"
        "model = 'discovered-model'\n"
        "fallback = [ { model = 'fb-one', provider = 'anthropic' } ]\n",
    )
    _pointer(tmp_path, "[llm.model_classes.frontier]\nfallback = []\n", monkeypatch)
    mc, slots = _slots(p)
    assert mc.resolve_fallback_chain("frontier", slots) == []


# ── the deep merge must NOT leak into shared config machinery ─────────────────────────
def test_read_reserved_section_stays_shallow_for_a_non_llm_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`read_reserved_section` is shared by EVERY reserved section (llm, snapshot). Its
    user-then-project merge is SHALLOW, and deepening it would silently change layering for
    unrelated consumers. Pin the shallow behaviour for `snapshot`: a project sub-table
    REPLACES the user one, dropping the user's sibling key."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[snapshot.nested]\nshared = 'project'\n", encoding="utf-8")
    xdg = tmp_path / "xdg"
    (xdg / "rebar").mkdir(parents=True)
    (xdg / "rebar" / "config.toml").write_text(
        "[snapshot.nested]\nshared = 'user'\nuser_only = 'user'\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    cfg.reset_config_cache()
    merged = cfg.read_reserved_section("snapshot", p)
    assert merged["nested"] == {"shared": "project"}


def test_the_pointer_does_not_apply_to_other_reserved_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pointer is scoped to the LLM layer — its name says so. A `[snapshot]` table in
    the pointed file must not silently become snapshot configuration."""
    p = _proj(tmp_path)
    (p / "rebar.toml").write_text("[snapshot]\nmode = 'discovered'\n", encoding="utf-8")
    _pointer(tmp_path, "[snapshot]\nmode = 'pointed'\n", monkeypatch)
    cfg.reset_config_cache()
    assert cfg.read_reserved_section("snapshot", p) == {"mode": "discovered"}


# ── the doc gate: the read must be a STRING LITERAL ───────────────────────────────────
def _gen_env_registry():
    path = REPO_ROOT / "scripts" / "gen_env_registry.py"
    spec = importlib.util.spec_from_file_location("gen_env_registry_cb6f", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_pointer_is_statically_visible_to_the_env_registry_generator() -> None:
    """`docs/env-vars.md` is DERIVED by a static AST scanner that only records a
    string-LITERAL `os.environ.get("...")` argument, and CI fails on drift. An f-string or
    variable read passes every behavioural test above while the variable never reaches the
    documented registry — exactly what happened on sibling task f844."""
    gen = _gen_env_registry()
    reads, _dynamic = gen.scan(gen.DEFAULT_SCAN_ROOT)
    assert "REBAR_LLM_CONFIG_FILE" in reads


def test_the_committed_env_var_registry_lists_the_pointer() -> None:
    """The regenerated `docs/env-vars.md` must actually be COMMITTED with the variable in
    its table — a statically visible read plus a stale committed doc is still a red CI."""
    doc = (REPO_ROOT / "docs" / "env-vars.md").read_text(encoding="utf-8")
    assert "`REBAR_LLM_CONFIG_FILE`" in doc
