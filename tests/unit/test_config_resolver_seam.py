"""Seam invariants for the ``_config_sources`` / ``_config_resolvers`` split (story 1a33).

``src/rebar/_config_sources.py`` was split at the boundary its own source already draws:
the RAW-INPUT resolution layer (repo-root + config-file location, the mtime-keyed TOML
parse cache, project/user discovery, the ``REBAR_<SECTION>_<KEY>`` env-override layer)
stays put, and the RP-04 below-seam OWNED RESOLVERS -- the banner-delimited blocks the
file labelled "Below-seam config resolvers" (d074), "Below-seam CLI/command resolvers"
(9515), "Below-seam LLM-subsystem resolvers" (1b07) and "Reconciler JIRA-family
resolvers" -- move to :mod:`rebar._config_resolvers`.

Three invariants are pinned here, each of which the move could plausibly have broken:

1. **Import surface.** Every moved name still resolves through ``rebar._config_sources``
   AND ``rebar.config``, because both re-export the sibling. A consumer that imported a
   resolver from either module keeps working.
2. **Late binding across the move.** The one moved body that reaches back into the
   staying half -- ``_snapshot_table`` -- resolves ``user_config_path`` /
   ``_read_toml_table`` / ``_discover_project_config`` at CALL time via a lazy in-body
   ``from rebar import _config_sources``, so a ``monkeypatch.setattr`` on
   ``_config_sources`` still steers it. Binding those names eagerly at import time is
   exactly the regression that shipped in the sibling ``config.py`` split (Gerrit 1932),
   where a patched ``repo_root`` silently stopped applying and the code wrote outside
   the test's tmp root.
3. **Acyclic stdlib-only leaf.** ``_config_resolvers`` carries NO module-level ``rebar``
   import (the reconciler family imports ``rebar.config`` lazily in-body), which is both
   what keeps the pair acyclic while ``_config_sources`` re-exports it, and what lets the
   stdlib-only reconciler engine import it without ``rebar`` fully initialised.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from rebar import _config_resolvers, _config_sources
from rebar import config as cfg

pytestmark = pytest.mark.unit

# The complete moved set: 23 ``resolve_*`` entry points + ``repo_root_or_none``
# (24 owned entry points), their four private helpers, and one constant.
MOVED_ENTRY_POINTS = (
    "repo_root_or_none",
    "resolve_absent_retire_grace",
    "resolve_acli_call_timeout",
    "resolve_allow_env_reidentify",
    "resolve_dc_comment_max_chars",
    "resolve_dc_connection",
    "resolve_detected_by",
    "resolve_gate_ref",
    "resolve_gate_source",
    "resolve_gate_tmpdir",
    "resolve_janitor_tunables",
    "resolve_jira_connection",
    "resolve_jira_probe_scope",
    "resolve_lock_retries",
    "resolve_os_actor",
    "resolve_otlp_endpoint",
    "resolve_pandoc_timeout",
    "resolve_plan_review_budget",
    "resolve_preview_timeout",
    "resolve_rich_text_cutover",
    "resolve_run_root",
    "resolve_stall_abort_limits",
    "resolve_stall_attempts",
    "resolve_usage_log_sink",
)
MOVED_PRIVATE = (
    "_DEFAULT_ABSENT_RETIRE_GRACE",
    "_gate_str_pref",
    "_positive_int",
    "_snapshot_int",
    "_snapshot_table",
)
# Raw-input names that must NOT have moved -- the other side of the seam.
STAYING = (
    "_TOML_CACHE",
    "_canonical_env_name",
    "_deep_merge",
    "_discover_project_config",
    "_parse_toml",
    "_pyproject_rebar_state",
    "_read_toml_table",
    "config_file",
    "env_overrides",
    "layer_llm_config_file",
    "llm_config_file_pointer",
    "repo_root",
    "tracker_dir_override",
    "user_config_path",
)


# ── 1. import surface ────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", MOVED_ENTRY_POINTS + MOVED_PRIVATE)
def test_moved_name_still_resolves_through_config_sources(name: str) -> None:
    """No symbol previously reachable as ``rebar._config_sources.<name>`` stops resolving."""
    assert getattr(_config_sources, name) is getattr(_config_resolvers, name)


@pytest.mark.parametrize("name", MOVED_ENTRY_POINTS)
def test_moved_entry_point_still_resolves_through_rebar_config(name: str) -> None:
    """``rebar.config`` is the public composition root; its re-export must survive."""
    assert getattr(cfg, name) is getattr(_config_resolvers, name)


@pytest.mark.parametrize("name", STAYING)
def test_raw_input_half_did_not_move(name: str) -> None:
    """The staying half is DEFINED in ``_config_sources``, not re-exported from the sibling."""
    assert hasattr(_config_sources, name)
    assert not hasattr(_config_resolvers, name), f"{name} belongs to the raw-input half"


# ── 2. late binding across the move (the Gerrit-1932 regression class) ───────
def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "rebar.toml").write_text("[snapshot]\nref = 'patched-ref'\n", encoding="utf-8")
    return root


def test_snapshot_table_honours_a_patched_discover_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A patch on ``_config_sources._discover_project_config`` still steers the MOVED
    ``_snapshot_table``. Eager import-time binding in ``_config_resolvers`` breaks this."""
    root = _project(tmp_path)
    calls: list[object] = []

    real = _config_sources._discover_project_config

    def _spy(arg=None):  # test double mirrors the real signature
        calls.append(arg)
        return real(root)

    monkeypatch.setattr(_config_sources, "_discover_project_config", _spy)
    assert _config_resolvers._snapshot_table() == {"ref": "patched-ref"}
    assert calls, "the patched _discover_project_config was never reached"


def test_snapshot_table_honours_a_patched_user_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same for ``user_config_path`` -- the user layer of the merged ``[snapshot]`` table."""
    user_cfg = tmp_path / "user.toml"
    user_cfg.write_text("[snapshot]\ngrace_seconds = 77\n", encoding="utf-8")
    monkeypatch.setattr(_config_sources, "user_config_path", lambda: user_cfg)
    monkeypatch.setattr(_config_sources, "_discover_project_config", lambda root=None: None)
    assert _config_resolvers._snapshot_table() == {"grace_seconds": 77}


def test_patched_read_toml_table_reaches_the_moved_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third raw-input name ``_snapshot_table`` calls, exercised through the public
    ``resolve_gate_ref`` so the whole moved call chain is covered end to end."""
    user_cfg = tmp_path / "user.toml"
    user_cfg.write_text("[snapshot]\nref = 'from-user'\n", encoding="utf-8")
    monkeypatch.delenv("REBAR_GATE_REF", raising=False)
    monkeypatch.setattr(_config_sources, "user_config_path", lambda: user_cfg)
    monkeypatch.setattr(_config_sources, "_discover_project_config", lambda root=None: None)
    monkeypatch.setattr(
        _config_sources,
        "_read_toml_table",
        lambda path, *, pyproject: {"snapshot": {"ref": "patched-table"}},
    )
    assert _config_resolvers.resolve_gate_ref("fallback") == "patched-table"


# ── 3. acyclic stdlib-only leaf ──────────────────────────────────────────────
def test_config_resolvers_has_no_module_level_rebar_import() -> None:
    """No top-level ``rebar`` import: that is what keeps the re-export in
    ``_config_sources`` from forming an import cycle, and what lets the stdlib-only
    reconciler engine import this module. In-body imports are fine."""
    tree = ast.parse(Path(inspect.getsourcefile(_config_resolvers) or "").read_text("utf-8"))
    offenders = []
    for node in tree.body:  # module level ONLY -- in-body imports are not in tree.body
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "rebar"]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "rebar":
            offenders.append(node.module or "")
    assert offenders == [], f"module-level rebar imports would cycle: {offenders}"


def test_config_resolvers_is_a_recognised_composition_root() -> None:
    """The split half stays INSIDE the RP-04 config-ownership seam: the gate classifies
    composition roots by module basename, so the new basename must be listed or every
    owned env read in it would fire as a below-seam violation."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_gate_mod", Path(__file__).resolve().parents[2] / "scripts" / "check_config_ownership.py"
    )
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    assert "_config_resolvers.py" in gate.COMPOSITION_ROOT_BASENAMES
