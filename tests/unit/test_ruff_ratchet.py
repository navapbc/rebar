"""The shared Ruff-config quality-ratchet contract (story 28cd).

This file OWNS the assertions that keep the ASYNC/DTZ ratchet — and the surrounding
"no security-scanner sprawl" and "py310-safe UTC spelling" invariants — from silently
regressing. Each assertion is INDEPENDENT (no bundling with ``and``) so a failure names
exactly one violated invariant:

- ``[tool.ruff.lint].select`` enables the ``ASYNC`` family.
- ``[tool.ruff.lint].select`` enables the ``DTZ`` family.
- ``[tool.ruff.lint].select`` enables the ``RUF`` family, with the ambiguous-unicode
  families ``RUF001``/``RUF002``/``RUF003`` deferred in exactly ``[tool.ruff.lint].ignore``
  (no ``extend-ignore``, and no ``per-file-ignores`` entry selecting ``RUF``) — story 125d.
- the ``S`` (flake8-bandit security) selector is NOT enabled — security scanning is
  handled by the dedicated grounding detectors, not Ruff's ``S`` family.
- no root-level semgrep config artifact (``.semgrep.yml`` / ``.semgrep.yaml`` /
  ``.semgrep/``) exists — same "one security surface" invariant.
- no ``src/rebar`` source spells the literal token ``datetime.UTC``: Ruff
  ``target-version`` stays ``py310``, on which ``datetime.UTC`` does not exist, so aware
  UTC must be spelled ``datetime.timezone.utc`` / ``timezone.utc``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

pytestmark = pytest.mark.unit

# repo root = the directory containing pyproject.toml (tests/unit/<file> -> parents[2]).
REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SRC_REBAR = REPO_ROOT / "src" / "rebar"


def _ruff_lint_table() -> dict:
    """Return the ``[tool.ruff.lint]`` table from pyproject.toml (empty dict if absent)."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data.get("tool", {}).get("ruff", {}).get("lint", {})


def _ruff_select() -> list[str]:
    """Return the Ruff lint ``select`` list from pyproject.toml.

    Prefer ``[tool.ruff.lint].select`` (the current schema); fall back to the legacy
    ``[tool.ruff].select`` location if that is where it lives.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    ruff = data.get("tool", {}).get("ruff", {})
    lint = ruff.get("lint", {})
    if "select" in lint:
        return list(lint["select"])
    return list(ruff.get("select", []))


def test_pyproject_exists() -> None:
    assert PYPROJECT.is_file(), f"expected pyproject.toml at repo root {REPO_ROOT}"


def test_ruff_select_enables_async() -> None:
    assert "ASYNC" in _ruff_select(), "the ASYNC rule family must be enabled in ruff select"


def test_ruff_select_enables_dtz() -> None:
    assert "DTZ" in _ruff_select(), "the DTZ rule family must be enabled in ruff select"


def test_ruff_select_enables_ruf() -> None:
    assert "RUF" in _ruff_select(), (
        "the RUF (Ruff-native) rule family must be enabled in ruff select (story 125d)"
    )


def test_ruff_ignore_is_exactly_the_ruf_unicode_deferral() -> None:
    # The global RUF deferral is pinned to exactly RUF001/RUF002/RUF003 (ambiguous-unicode
    # families that flag intentional prose / UI content). Nothing else is globally ignored.
    ignore = list(_ruff_lint_table().get("ignore", []))
    assert ignore == ["RUF001", "RUF002", "RUF003"], (
        "[tool.ruff.lint].ignore must be exactly ['RUF001', 'RUF002', 'RUF003'] "
        f"(the deferred ambiguous-unicode families), got {ignore!r}"
    )


def test_ruff_lint_has_no_extend_ignore() -> None:
    # The deferral lives only in the exact `ignore` key; `extend-ignore` would be a second,
    # drift-prone place to silence rules.
    assert "extend-ignore" not in _ruff_lint_table(), (
        "[tool.ruff.lint] must not define `extend-ignore`; the only global deferral key is "
        "`ignore` (story 125d)"
    )


def test_no_per_file_ignore_selects_ruf() -> None:
    # Deterministically prohibit broad or code-specific RUF per-file exemptions: no configured
    # per-file-ignores code may equal `RUF` or start with `RUF` (e.g. RUF100, RUF012).
    per_file = _ruff_lint_table().get("per-file-ignores", {})
    offenders = {
        pattern: [code for code in codes if code == "RUF" or code.startswith("RUF")]
        for pattern, codes in per_file.items()
        if any(code == "RUF" or code.startswith("RUF") for code in codes)
    }
    assert not offenders, (
        "no per-file-ignores entry may select the RUF family or a RUF code; the RUF deferral "
        f"belongs only in the global `ignore` key. Offending patterns: {offenders}"
    )


def test_ruff_select_does_not_enable_bandit_security() -> None:
    assert "S" not in _ruff_select(), (
        "the flake8-bandit `S` selector must NOT be enabled — security scanning is the "
        "grounding detectors' job, not Ruff's"
    )


def test_no_root_semgrep_yml() -> None:
    assert not (REPO_ROOT / ".semgrep.yml").exists(), "no root .semgrep.yml may exist"


def test_no_root_semgrep_yaml() -> None:
    assert not (REPO_ROOT / ".semgrep.yaml").exists(), "no root .semgrep.yaml may exist"


def test_no_root_semgrep_dir() -> None:
    assert not (REPO_ROOT / ".semgrep").exists(), "no root .semgrep/ directory may exist"


def test_no_src_uses_datetime_UTC_token() -> None:
    # target-version stays py310, where datetime.UTC does not exist; require the
    # datetime.timezone.utc spelling everywhere under src/rebar.
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted(SRC_REBAR.rglob("*.py"))
        if "datetime.UTC" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "the literal token `datetime.UTC` is forbidden under src/rebar (target-version is "
        f"py310); use `datetime.timezone.utc`. Offending files: {offenders}"
    )
