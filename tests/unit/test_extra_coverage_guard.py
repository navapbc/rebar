"""Bug 599e-77da-29dd-482d: a test surface must not be able to vanish from CI unnoticed.

`pytest.importorskip("fastapi")` is the right call for a suite that must stay runnable in a
lean install — but it also means the ONLY thing standing between a security test and total
invisibility is a CI install step remembering an extra. Nothing reported when that memory
failed: no lane installed the `reviewbot` extra, so 38 tests (the review-bot receiver, the
opcert service app, the audit UI, and the path-injection + token-redaction guards) skipped in
every run for months, and a change that broke one of them still earned `Verified +1`.

Two halves are tested here:

  * the RUNTIME guard — with `REBAR_REQUIRE_EXTRAS=1`, `tests/conftest.py` replaces
    `pytest.importorskip` with a strict variant that RAISES instead of skipping, so the build
    reddens by module name (collection error for a module-scope call, test error for a
    function-scope one);
  * the CI WIRING — the workflow lane that runs pytest actually installs those extras and
    actually sets that env var, so the guard is armed where it matters.

Deliberately NOT covered by the guard: skips keyed on a missing binary or a version floor
(`ssh-keygen >= 8.9`, Node for the e2e tier) use `pytest.mark.skipif`, not `importorskip`.
"""

from __future__ import annotations

import re
from pathlib import Path

import _extra_guard
import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_BAT_YML = _ROOT / ".github" / "workflows" / "_build-and-test.yml"


# Every extra whose absence silently disarms tests. `dev` resolves uvicorn/httpx/boto3
# transitively today, but `fastapi` (reviewbot + ui) and `jinja2` (ui) come only from these.
_TEST_BEARING_EXTRAS = ("dev", "reviewbot", "ui")


def test_strict_importorskip_returns_the_module_when_it_is_installed() -> None:
    """The strict variant is a drop-in: an installed module still imports normally."""
    import json

    assert _extra_guard.strict_importorskip("json") is json


def test_strict_importorskip_raises_instead_of_skipping_when_absent() -> None:
    """The whole point: a missing optional dep must REDDEN the build, not skip quietly."""
    with pytest.raises(ImportError) as excinfo:
        _extra_guard.strict_importorskip("rebar_no_such_module_599e")

    message = str(excinfo.value)
    # The message must name the module and point at the install step — a guard that fires
    # without saying which extra to add just moves the mystery.
    assert "rebar_no_such_module_599e" in message
    assert "_build-and-test.yml" in message
    assert "REBAR_REQUIRE_EXTRAS" in message
    # It must NOT be a skip: `pytest.skip.Exception` would be silently swallowed as a skip
    # by pytest, which is the exact failure mode being fixed.
    assert not isinstance(excinfo.value, pytest.skip.Exception)


def test_guard_is_installed_exactly_when_the_env_var_is_set() -> None:
    """The patch is wired to `REBAR_REQUIRE_EXTRAS`, in both directions."""
    if _extra_guard.required():
        assert pytest.importorskip is _extra_guard.strict_importorskip
    else:
        assert pytest.importorskip is _extra_guard.real_importorskip


@pytest.mark.parametrize("extra", _TEST_BEARING_EXTRAS)
def test_the_pytest_lane_installs_every_test_bearing_extra(extra: str) -> None:
    """The lane that RUNS the suite must install the extras that suite's tests need."""
    body = _BAT_YML.read_text(encoding="utf-8")
    install = re.search(r"^\s*uv sync --locked .*--python .*$", body, re.MULTILINE)
    assert install is not None, "the pytest job's `uv sync` install step disappeared"
    assert f"--extra {extra}" in install.group(0), (
        f"the pytest lane no longer installs the `{extra}` extra — every test gated on it "
        f"will silently SKIP in CI (bug 599e). Install step: {install.group(0).strip()}"
    )


def test_both_pytest_steps_arm_the_guard() -> None:
    """Installing the extras is not enough; the env var is what detects losing them again."""
    body = _BAT_YML.read_text(encoding="utf-8")
    pytest_invocations = re.findall(r"^\s*pytest -m ", body, re.MULTILINE)
    armed = re.findall(r'^\s*REBAR_REQUIRE_EXTRAS: "1"', body, re.MULTILINE)
    assert pytest_invocations, "no pytest invocation found in the reusable workflow"
    assert len(armed) == len(pytest_invocations), (
        f"{len(pytest_invocations)} pytest step(s) but {len(armed)} armed with "
        "REBAR_REQUIRE_EXTRAS=1 — an unarmed suite can lose an extra silently (bug 599e)"
    )
