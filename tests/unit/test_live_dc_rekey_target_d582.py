"""The live DC rekey cell's rename-target derivation cannot return the source key (bug d582).

REPO-ONLY BY CONSTRUCTION — no Jira, no credentials, no network. That is the point: the defect
this pins was a 1-in-26 flake that only ever surfaced on a live harness run (30772461871, which
drew ``RBJDRDZ``), so the guard has to live somewhere that runs on every commit instead of
somewhere that runs when a random draw cooperates.

``_dc_support`` is loaded BY PATH rather than imported by name because ``tests/external/`` is not
a package on ``sys.path`` for the unit tier — pytest only puts that directory on the path when it
collects the external suite itself.
"""

from __future__ import annotations

import importlib.util
import string
from pathlib import Path
from types import ModuleType

import pytest

_SUPPORT = Path(__file__).resolve().parents[1] / "external" / "live_jira_dc" / "_dc_support.py"

# Mirrors tests/external/live_jira_dc/conftest.py `_PROJECT_KEY_PREFIX` + `_random_project_key`:
# every harness project key is this prefix plus four random uppercase letters.
_PREFIX = "RBJ"
_KEY_LETTERS = 4


def _load_dc_support() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_dc_support_d582", _SUPPORT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dc_support() -> ModuleType:
    return _load_dc_support()


@pytest.mark.parametrize("final", list(string.ascii_uppercase))
def test_the_rename_target_never_equals_the_source_key(dc_support, final):
    """Every possible final letter, so the Z case is covered by enumeration, not by luck.

    The pre-fix derivation appended a fixed "Z" to the truncated key, which returns the source
    key unchanged for exactly one of these 26 inputs. Parametrizing over the whole alphabet is
    what makes that case impossible to regress silently.
    """
    source = f"{_PREFIX}{'A' * (_KEY_LETTERS - 1)}{final}"
    assert len(source) == len(_PREFIX) + _KEY_LETTERS

    target = dc_support.derive_rename_target(source)

    assert target != source, (
        f"derive_rename_target({source!r}) returned the SOURCE key — the rekey cell would "
        f"rename the project to the key it already has and then assert about a key that was "
        f"never stale"
    )


def test_the_key_ending_in_the_replacement_character_is_covered_explicitly(dc_support):
    """The exact shape that failed live: a key already ending in the replacement character.

    Named separately from the parametrized sweep so the regression this ticket exists for is
    legible in the test report rather than hidden as one id among 26.
    """
    assert dc_support.derive_rename_target("RBJDRDZ") == "RBJDRDY"


@pytest.mark.parametrize("final", list(string.ascii_uppercase))
def test_the_derived_key_still_satisfies_jiras_project_key_rules(dc_support, final):
    """Asserted, not assumed: Jira project keys are 2-10 chars, uppercase, leading letter."""
    source = f"{_PREFIX}{'A' * (_KEY_LETTERS - 1)}{final}"

    target = dc_support.derive_rename_target(source)

    assert 2 <= len(target) <= 10, f"{target!r} is outside Jira's 2-10 character key length"
    assert target.isupper(), f"{target!r} is not uppercase"
    assert target.isalpha(), f"{target!r} must be letters only for a harness-generated key"
    assert target[0] in string.ascii_uppercase, f"{target!r} must start with a letter"
    # The derivation swaps the final character rather than growing the key, so a rename can
    # never push a legal key past Jira's ceiling.
    assert len(target) == len(source), f"{target!r} changed length from {source!r}"


def test_the_live_cell_uses_the_shared_derivation_rather_than_its_own_copy():
    """Guards the fix's single-sourcing: a re-inlined literal "Z" would reintroduce the flake.

    The cell asserting `new_project_key != jira_dc_project` is retained deliberately (it is what
    caught the original collision), so this checks the DERIVATION is shared, not that the
    assertion is gone.
    """
    cell = (
        Path(__file__).resolve().parents[1] / "external" / "live_jira_dc" / "test_transport.py"
    ).read_text(encoding="utf-8")

    assert "derive_rename_target(jira_dc_project)" in cell, (
        "the rekey cell no longer derives its target through _dc_support.derive_rename_target"
    )
    assert "from _dc_support import derive_rename_target" in cell
    # The safety-net assertion the ticket requires be RETAINED.
    assert "assert new_project_key != jira_dc_project" in cell
