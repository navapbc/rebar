"""Bug 049e-9fac-a821-4ea2 — DC's comment ceiling is ADMIN-CONFIGURABLE, so rebar
must not hardcode the default.

Jira Data Center governs comment length with the advanced setting
``jira.text.field.character.limit`` (Jira's own ``jpm.xml``: default 32767, scope
"Description, Environment, Comments and Text custom fields", ``0`` = unlimited,
documented maximum 2147483647; JRASERVER-28519 records 7.0.0 making 32767 the
default). rebar truncated at the compiled-in 32767, so on an instance where an
administrator RAISED the limit — or set it to ``0`` — rebar silently dropped text
Jira would have accepted in full.

These tests pin the four observable behaviours of the fix, always on the RESULTING
BODY (length + content), never on "no exception raised":

* a ceiling configured UPWARD is honored — the body is returned whole (the RED test);
* ``0`` means unlimited;
* a ceiling configured DOWNWARD is honored;
* with nothing configured the default is still the stock 32767.

Plus the decoupling AC: ``sanitize_comment`` must no longer route through the
DESCRIPTION fitter (``WikiTextCodec.fit_outbound``), so a future format-aware change
to description fitting cannot silently retarget comments (Cloud is the cautionary
case — its description fitter measures ADF-SERIALIZED size).

Every expected length here is a MODULE-LOCAL LITERAL, deliberately not derived from
the constant under test: an expectation imported from the code under test moves with
that code and can never fail.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from rebar import config as cfg
from rebar_reconciler.adapters.jira_datacenter.backend import _DCSanitizer
from rebar_reconciler.adapters.jira_family.rich_text import WikiTextCodec

pytestmark = pytest.mark.unit

#: The stock ``jira.text.field.character.limit`` default (jpm.xml / JRASERVER-28519).
_STOCK_CEILING = 32767
#: A plausible raised value an administrator may have set.
_RAISED_CEILING = 100_000
#: A body that the STOCK ceiling truncates but the RAISED ceiling accepts whole.
_LONG_BODY_LEN = 50_000
#: Auto-derived canonical env override for ``[tool.rebar.reconciler].comment_max_chars``.
_ENV_KEY = "REBAR_RECONCILER_COMMENT_MAX_CHARS"


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Resolve config against an EMPTY temp project, so the developer's own
    checkout/user config cannot decide the ceiling under test."""
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.setenv("REBAR_ROOT", str(proj))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("REBAR_CONFIG", raising=False)
    monkeypatch.delenv(_ENV_KEY, raising=False)
    cfg.reset_config_cache()
    yield
    cfg.reset_config_cache()


def _configure(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    monkeypatch.setenv(_ENV_KEY, str(value))
    cfg.reset_config_cache()


# ── the bug: a raised ceiling must be honored ─────────────────────────────────
def test_raised_ceiling_returns_the_body_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, _RAISED_CEILING)
    body = "x" * _LONG_BODY_LEN
    out = _DCSanitizer().sanitize_comment(body)
    assert len(out) == _LONG_BODY_LEN, (
        f"comment was TRUNCATED to {len(out)} chars under a ceiling configured to "
        f"{_RAISED_CEILING}: rebar applied its compiled-in {_STOCK_CEILING} instead of the "
        "configured jira.text.field.character.limit, which is admin-settable on Data Center"
    )
    assert out == body


def test_zero_ceiling_means_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    """``0`` is jpm.xml's documented "unlimited" value, not "truncate everything"."""
    _configure(monkeypatch, 0)
    body = "y" * _LONG_BODY_LEN
    out = _DCSanitizer().sanitize_comment(body)
    assert out == body, f"a ceiling of 0 (unlimited) truncated to {len(out)} chars"


def test_lowered_ceiling_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ceiling configured DOWN must bite too — configurability, not just a raise."""
    _configure(monkeypatch, 500)
    out = _DCSanitizer().sanitize_comment("z" * 4000)
    assert len(out) == 500, f"expected a 500-char ceiling to be applied, got {len(out)}"
    assert out.startswith("z")


def test_unconfigured_default_is_still_the_stock_ceiling() -> None:
    """The DEFAULT must not move: 32767 is correct for a stock Jira >= 7.0."""
    body = "q" * _LONG_BODY_LEN
    out = _DCSanitizer().sanitize_comment(body)
    assert len(out) == _STOCK_CEILING, (
        f"unconfigured DC comment ceiling resolved to {len(out)} chars; the stock "
        f"jira.text.field.character.limit default is {_STOCK_CEILING}"
    )
    assert out != body
    assert out.startswith("q")


# ── the decoupling AC: no dependency on the DESCRIPTION fitter ────────────────
def test_sanitize_comment_does_not_call_the_description_fitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sanitize_comment`` must not route through ``WikiTextCodec.fit_outbound``.

    Poison the description fitter: if the comment path still depends on it, the
    poisoned marker (or the description ceiling) shows up in the RESULT.
    """
    monkeypatch.setattr(
        WikiTextCodec, "fit_outbound", lambda self, text: "POISONED-DESCRIPTION-FITTER"
    )
    _configure(monkeypatch, _RAISED_CEILING)
    body = "w" * _LONG_BODY_LEN
    out = _DCSanitizer().sanitize_comment(body)
    assert out == body, (
        "sanitize_comment still depends on the description fitter: poisoning "
        f"WikiTextCodec.fit_outbound changed the comment result to {out[:40]!r}"
    )


def test_fit_comment_converges_with_sanitize_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The differ-side ``fit_comment`` must apply the IDENTICAL transform as the
    send-side ``sanitize_comment`` under the SAME configured ceiling, or the
    outbound comment loop re-emits forever (the convergence contract)."""
    _configure(monkeypatch, 4096)
    sanitizer = _DCSanitizer()
    body = "c" * _LONG_BODY_LEN
    assert sanitizer.fit_comment(body) == sanitizer.sanitize_comment(body)
    assert len(sanitizer.fit_comment(body)) == 4096
