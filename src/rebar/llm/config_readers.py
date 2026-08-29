"""Config-file / env resolution helpers for :mod:`rebar.llm.config` (ticket 02b7 moved
these seven functions out to clear the 800-line module-size cap; see that module's
docstring for the module-size policy this relieves).

These are pure relocation targets — no logic changed. They are re-imported into
``rebar.llm.config`` (see that module's re-export block) so every existing caller
and every ``monkeypatch.setattr(rebar.llm.config, "...")`` target keeps working
unchanged; this module is not meant to be imported directly by consumers.
"""

from __future__ import annotations

import os

from rebar import config as _root_config
from rebar.llm.gate_context import current_code_root


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


# ── [tool.rebar.llm] config-file layer (0ac6 slice 4) ─────────────────────────
#
# llm.* is resolved HERE (not the stdlib-core typed Config) so importing rebar.llm
# never pulls the agents stack into core. The non-secret, non-runtime, non-derived
# knobs are settable in a ``[tool.rebar.llm]`` table (pyproject / rebar.toml [llm] /
# XDG user config), read via the core loader's
# discovery so file LOCATIONS + precedence match the rest of rebar. Resolution per
# key: ``rebar -c llm.KEY=VALUE`` (CLI) > ``REBAR_LLM_<KEY>`` env > config file >
# default. Secrets (REBAR_LLM_API_KEY / ANTHROPIC/OPENAI keys / LANGFUSE_*),
# the runtime-only REBAR_LLM_REPO_PATH, and the DERIVED runner stay env-only and
# are NOT config-file keys.


def _read_llm_file_table(repo_root=None) -> dict:
    """The merged ``[tool.rebar.llm]`` table (user < project < ``REBAR_LLM_CONFIG_FILE``),
    or ``{}``. A malformed core config degrades to env-only — a broken pyproject must never
    break an LLM op — but the ``REBAR_LLM_CONFIG_FILE`` pointer is layered OUTSIDE that
    degrade, so an environment that explicitly named a config file is never silently
    ignored (it deep-merges per key over the discovered table; see
    :func:`rebar.config.layer_llm_config_file`).

    A running gate PINS its code read-root (``use_code_root``, an attested snapshot), and
    ``gate_source.gate_read_root`` states the invariant that EVERY config rebuilt deep in the
    gate reads that root. Discovery below runs through ``rebar.config``, whose ``repo_root()``
    only knows ``explicit > REBAR_ROOT > git toplevel of CWD`` — so a caller threading no root
    resolved against the AMBIENT cwd and, when that missed the project's ``[tool.rebar.llm]``
    table, silently handed :func:`rebar.llm.model_classes.parse_class_slots` ``{}``, routing a
    Bedrock-configured project at the built-in DIRECT-ANTHROPIC class defaults (bug 2876).
    Defaulting to the active gate root fixes every such reader at once — the model-class table
    and the scalar ``LLMConfig.from_env().model`` alike — because both land here. An EXPLICIT
    ``repo_root`` still wins, so threading a root never becomes a no-op, and outside a gate
    ``current_code_root()`` is ``None`` and ambient discovery is unchanged.
    """
    if repo_root is None:
        repo_root = current_code_root()
    try:
        discovered = _root_config.read_reserved_section("llm", repo_root)
    except _root_config.ConfigError:
        discovered = {}
    return _root_config.layer_llm_config_file(discovered)


def _llm_str_source(table: dict, cli: dict, env_name: str | None, file_key: str, default):
    """Resolve a string setting to ``(value, source)``: CLI > env > file > default (blank →
    fall through). The ``source`` labels the layer that actually won — ``"cli"``, the env
    var's own name, ``"repo-config"`` for the config-file table, or ``None`` for the default —
    from the SAME pass that produced the value, so a provenance label derived from it can
    never diverge from the resolution (cda8). ``env_name=None`` means the setting has NO env
    channel (CLI + config file only) — used by ``model``, whose bare ``REBAR_LLM_MODEL`` env
    was removed and tombstoned."""
    if file_key in cli and str(cli[file_key]).strip():
        return str(cli[file_key]).strip(), "cli"
    raw = os.environ.get(env_name) if env_name is not None else None
    if raw is not None and raw.strip():
        return raw.strip(), env_name
    fv = table.get(file_key)
    if fv is not None and str(fv).strip():
        return str(fv).strip(), "repo-config"
    return default, None


def _llm_str(table: dict, cli: dict, env_name: str | None, file_key: str, default):
    """Resolve a string setting: CLI > env > file > default (blank → fall through).

    Delegates to :func:`_llm_str_source` (the ONE resolution pass) and drops the source —
    identical value semantics for every existing key."""
    return _llm_str_source(table, cli, env_name, file_key, default)[0]


def _llm_int(table: dict, cli: dict, env_name: str, file_key: str, default: int):
    """Resolve an int setting: CLI > env > file > default. An unparseable higher
    layer falls through to the next."""
    candidates: list = []
    if file_key in cli:
        candidates.append(cli[file_key])
    env_raw = os.environ.get(env_name)
    if env_raw is not None and env_raw.strip():
        candidates.append(env_raw)
    fv = table.get(file_key)
    if fv is not None and not isinstance(fv, bool):
        candidates.append(fv)
    for c in candidates:
        try:
            return int(str(c).strip())
        except (TypeError, ValueError):
            continue
    return default


def _llm_float(table: dict, cli: dict, env_name: str, file_key: str, default: float | None):
    """Resolve a float setting: CLI > env > file > default. An unparseable higher
    layer falls through to the next (mirrors :func:`_llm_int`)."""
    candidates: list = []
    if file_key in cli:
        candidates.append(cli[file_key])
    env_raw = os.environ.get(env_name)
    if env_raw is not None and env_raw.strip():
        candidates.append(env_raw)
    fv = table.get(file_key)
    if fv is not None and not isinstance(fv, bool):
        candidates.append(fv)
    for c in candidates:
        try:
            return float(str(c).strip())
        except (TypeError, ValueError):
            continue
    return default


def _llm_drain_mode(raw: str) -> str:
    """Validate the overlap_drain enum; an unrecognized value falls back to the default."""
    from rebar.llm.config import DEFAULT_OVERLAP_DRAIN

    v = str(raw).strip().lower()
    return v if v in ("off", "async", "always") else DEFAULT_OVERLAP_DRAIN
