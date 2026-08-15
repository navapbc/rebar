"""Story 5c0e — NOTICE must keep describing the GPL binary rebar actually ships.

The Data Center wiki renderer executes pandoc, which is GPL-2.0-or-later and
rides inside the third-party ``pypandoc-binary`` wheel. Anyone redistributing a
bundle that contains that executable — a container image, a wheel built with the
``wiki`` extra baked in — carries the GPL's source-availability obligation for
it, and the repo carried only ``LICENSE`` before this story.

An obligation is only discharged if the record matches what is actually shipped,
so the interesting failure is not a missing NOTICE (loud, obvious) but a SILENT
drift: someone bumps the wheel pin and the recorded version quietly becomes a
statement about a binary nobody distributes any more. That is what these bind
together.

CI runs an equivalent gate on the same facts. This exists as well as that one so
the check is runnable locally, before a push rather than after a ``Verified -1``,
and so the reasoning lives next to the assertions rather than in shell.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NOTICE = _REPO_ROOT / "NOTICE"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

_PIN_RE = re.compile(r"pypandoc-binary==([0-9][0-9A-Za-z.]*)")


def _notice_text() -> str:
    assert _NOTICE.is_file(), "NOTICE is missing — it records the bundled pandoc's GPL obligation"
    return _NOTICE.read_text(encoding="utf-8")


def _declared_pins() -> set[str]:
    """Every ``pypandoc-binary==<version>`` pin declared in pyproject.toml."""
    return set(_PIN_RE.findall(_PYPROJECT.read_text(encoding="utf-8")))


def test_notice_names_pandoc_its_version_and_its_licence() -> None:
    text = _notice_text()
    assert "pandoc" in text.lower()
    assert "3.9" in text, "NOTICE must name the bundled pandoc VERSION, not just the component"
    assert "GPL" in text, "NOTICE must record pandoc's licence"
    assert "pandoc.org" in text or "github.com/jgm/pandoc" in text, (
        "NOTICE must point at upstream so the GPL's source-availability obligation is actionable"
    )


def test_the_wheel_is_pinned_to_exactly_one_version() -> None:
    """A range would leave the shipped pandoc unknowable, so NOTICE could not describe it."""
    pins = _declared_pins()
    assert pins, "pyproject.toml must pin pypandoc-binary with '==' (a range is not a pin)"
    assert len(pins) == 1, f"pypandoc-binary is pinned to more than one version: {sorted(pins)}"


def test_notice_and_the_pin_cannot_drift_apart() -> None:
    """The load-bearing one: a pin bump must drag NOTICE with it.

    Version-checking each file separately would let them pass while describing
    different binaries, which is exactly the silent failure this guards.
    """
    (pin,) = _declared_pins()
    text = _notice_text()
    assert f"pypandoc-binary=={pin}" in text, (
        f"NOTICE does not mention the pinned wheel (pypandoc-binary=={pin}). Update NOTICE so "
        "the recorded pandoc provenance matches what is actually shipped."
    )


def test_notice_says_how_pandoc_reaches_an_install() -> None:
    """Whether the obligation applies at all depends on the optional extra.

    An install without ``wiki`` contains no pandoc, so a NOTICE that omitted this
    would overstate the obligation for most users and understate the condition
    under which it genuinely binds.
    """
    text = _notice_text()
    assert "wiki" in text, "NOTICE must say the pandoc binary arrives only via the `wiki` extra"
    assert "pypandoc-binary" in text
