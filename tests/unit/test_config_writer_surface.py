"""Keep config writer names available through the public facade.

``write_jira_config`` and ``_emit_toml`` must resolve through imports and
module attributes on ``rebar.config`` and be the same objects defined in
``rebar._config_writer``. Copies would fork the implementation."""

from __future__ import annotations

import rebar._config_writer as writer
import rebar.config as cfg
from rebar.config import _emit_toml, write_jira_config

_MOVED = ("write_jira_config", "_emit_toml")


def test_moved_names_are_importable_from_the_config_facade() -> None:
    """``from rebar.config import <name>`` keeps working for every moved symbol."""
    assert callable(write_jira_config)
    assert callable(_emit_toml)


def test_moved_names_are_reachable_as_config_module_attributes() -> None:
    """The module-attribute form (``_config.write_jira_config`` in the CLI wizard,
    ``cfg._emit_toml`` in the existing onboarding tests) still resolves."""
    for name in _MOVED:
        assert hasattr(cfg, name), f"rebar.config.{name} disappeared in the split"


def test_facade_re_exports_the_new_home_rather_than_a_copy() -> None:
    """Each facade name IS the object ``rebar._config_writer`` defines — a re-export, not a
    second implementation left behind by a copy-instead-of-move."""
    for name in _MOVED:
        assert getattr(cfg, name) is getattr(writer, name), (
            f"rebar.config.{name} is not the same object as rebar._config_writer.{name}"
        )


def test_the_write_path_lives_in_the_new_home() -> None:
    """The implementations were MOVED: both symbols' defining module is the new sibling."""
    for name in _MOVED:
        assert getattr(writer, name).__module__ == "rebar._config_writer"
