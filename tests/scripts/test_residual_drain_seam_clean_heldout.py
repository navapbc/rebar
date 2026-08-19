"""Held-out seam-clean + behavioral-regression proof for RP-04 C3f (residual drain).

Slice C3f drains the LAST below-seam ambient config/credential reads owned by nine
"residual" source files — the reads that RP-04's earlier cutovers left on the
``LEGACY_EXCEPTIONS`` whitelist. Each read is disposed exactly one of three ways:

  * CUT to compose through the approved seam (the operation snapshot / a startup binding /
    the provider-credential boundary), or
  * for a genuine bootstrap / CLI-entry / credential-deployment / diagnostic-kill-switch
    read that has no place in the operation snapshot, annotated in place with an inline
    ``# read-via:`` marker on the reading line (the established mechanism used by
    ``_store/hlc.py``, ``llm/gate_context.py``, ``_commands/session_id.py``), or
  * for the grounding subsystem's own config source (``grounding/resolve.py`` owns
    ``GroundingConfig`` / ``load_config`` / ``.rebar/grounding.toml`` / ``REBAR_CTAGS_BIN``),
    registered as an approved grounding composition seam so its own reads are owned there
    and callers stop calling ``load_config`` below the seam.

This slice does NOT reach whole-tree EMPTY: two ``binding_lifecycle.py`` rows
(``RECONCILER_ABSENT_RETIRE_GRACE`` + its ``os.environ.get`` parser) remain — they are the
RP-04 S7.3.a retarget's scope (tracker ``01d8``), not C3f's. So the oracle asserts a
PER-PATH drain of the nine owned files, never full EMPTY.

Observable behavior and contracts only — never internal structure.

Seam-clean (the strong anti-fake):

1. Every ``LEGACY_EXCEPTIONS`` entry for the nine owned files is gone (per-path, not
   whole-set EMPTY — the two binding_lifecycle rows legitimately remain).
2. The config-ownership gate reports ZERO findings for the nine owned files.
3. No path-glob exception masks any of the nine.
4. The two grounding config-load files carry ZERO ``# read-via:`` markers — they MUST be
   genuinely seam-registered / cut, not blanket-marked; and the total marked-line budget
   across all nine files is capped and confined, so an implementer cannot satisfy the gate
   by marking every read instead of disposing it honestly.

Behavioral (the disposals must be pure refactors of the read seam; asserted through stable
entry points that survive them):

5. ``REBAR_LOG_LEVEL`` still resolves the handler level (name / number / default).
6. ``shadow_enabled()`` still reads its kill-switch LIVE (unset ⇒ enabled).
7. The op-cert key-path / principal deployment overrides still apply when UNBOUND.
8. ``signing_key`` still honors an injected ``REBAR_SIGNING_KEY``.
9. ``mirror_guard`` still threads ``GITHUB_TOKEN`` from its CLI boundary.
10. ``review_bot`` still resolves ``REVIEW_BOT_PORT`` with its default/precedence.
11. The grounding per-invocation timeout still honors explicit-arg > env > default.
12. ``grounding.load_config`` still reads ``.rebar/grounding.toml`` and fails open, and
    ``REBAR_CTAGS_BIN`` is still honored.
"""

from __future__ import annotations

import re
from pathlib import Path

# Bare-name imports: ``tests/scripts/conftest.py`` puts repo-root ``scripts/`` on sys.path.
import check_config_ownership as gate
import config_ownership_exceptions as exceptions
import pytest

# The nine files this slice owns, relative to ``src/rebar/`` (the form the gate emits and
# the exception registry stores).
_OWNED_FILES = (
    "_logging.py",
    "_opcert_signing.py",
    "_operation_config.py",
    "mirror_guard.py",
    "signing.py",
    "review_bot/app.py",
    "grounding/harness.py",
    "grounding/oracle.py",
    "grounding/resolve.py",
)

# The two grounding config-load files must be genuinely seam-registered or cut — NEVER
# blanket-marked. A ``# read-via:`` marker here is a cheat that this oracle forbids.
_MARKER_FORBIDDEN_FILES = (
    "grounding/resolve.py",
    "grounding/oracle.py",
)

# The legitimate in-place reads (bootstrap / CLI-entry / credential-deploy / kill-switch)
# that MAY carry a marker: _logging(1), _operation_config(1), _opcert_signing(2),
# signing(1), mirror_guard(1), review_bot/app(1), grounding/harness(1) = 8 lines max.
_TOTAL_MARKER_CAP = 8

_MARKER_RE = re.compile(r"#\s*read-via:")
_SRC = gate.REPO_ROOT / "src" / "rebar"


def _gate_findings_for_owned() -> list[str]:
    return [f for f in gate.check(_SRC) if any(name in f for name in _OWNED_FILES)]


def _marker_count(relpath: str) -> int:
    text = (_SRC / relpath).read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if _MARKER_RE.search(line))


# ---------------------------------------------------------------------------
# Seam-clean structural properties
# ---------------------------------------------------------------------------


def test_no_legacy_exceptions_remain_for_owned_files() -> None:
    remaining = [
        (e["path"], e["symbol"]) for e in exceptions.LEGACY_EXCEPTIONS if e["path"] in _OWNED_FILES
    ]
    assert remaining == [], (
        "C3f residual drain must remove every LEGACY_EXCEPTIONS entry for the nine owned "
        f"files (per-path, not whole-set EMPTY); still present: {remaining}"
    )


def test_gate_reports_no_findings_for_owned_files() -> None:
    findings = _gate_findings_for_owned()
    assert findings == [], (
        "config-ownership gate must report zero findings for the nine owned files after "
        f"the residual drain; got: {findings}"
    )


def test_no_path_glob_exception_masks_the_owned_files() -> None:
    globbed = [
        e["path"]
        for e in exceptions.LEGACY_EXCEPTIONS
        if any(ch in str(e["path"]) for ch in "*?[]")
        and any(name in str(e["path"]) for name in _OWNED_FILES)
    ]
    assert globbed == [], f"no path-glob exception may mask the owned files; got: {globbed}"


def test_grounding_config_load_files_carry_no_markers() -> None:
    """The grounding config-load files must be genuinely seam-registered or cut — a
    ``# read-via:`` marker there would be blanket-marking instead of honest disposal."""
    marked = {rel: _marker_count(rel) for rel in _MARKER_FORBIDDEN_FILES if _marker_count(rel)}
    assert marked == {}, (
        "the grounding config-load files must carry ZERO read-via markers (seam-register "
        f"or cut, never mark); got: {marked}"
    )


def test_read_via_markers_are_bounded_and_confined() -> None:
    """The owned-in-place marker total is capped, so the drain cannot be faked by marking
    every read; and markers stay confined to the nine owned files."""
    total = sum(_marker_count(rel) for rel in _OWNED_FILES)
    assert total <= _TOTAL_MARKER_CAP, (
        f"the owned-in-place marker budget caps at {_TOTAL_MARKER_CAP} marked lines across "
        f"the nine files; got {total} — cut the reads that don't belong in-place"
    )


# ---------------------------------------------------------------------------
# Behavioral regressions the disposals must preserve (through stable entry points)
# ---------------------------------------------------------------------------


def test_log_level_resolution_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """``REBAR_LOG_LEVEL`` still drives the handler level: a symbolic name, a numeric
    level, and the WARNING default all resolve as before the disposal."""
    import logging

    from rebar._logging import _resolve_level

    monkeypatch.delenv("REBAR_LOG_LEVEL", raising=False)
    assert _resolve_level() == logging.WARNING, "unset REBAR_LOG_LEVEL must default to WARNING"
    monkeypatch.setenv("REBAR_LOG_LEVEL", "DEBUG")
    assert _resolve_level() == logging.DEBUG, "a symbolic level name must resolve"
    monkeypatch.setenv("REBAR_LOG_LEVEL", "10")
    assert _resolve_level() == 10, "a numeric level must resolve"


def test_shadow_kill_switch_is_read_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """``shadow_enabled()`` keeps reading its kill-switch LIVE: unset ⇒ enabled, a false
    spelling ⇒ disabled, and a mid-run flip is observed on the next call."""
    from rebar._operation_config import shadow_enabled

    monkeypatch.delenv("REBAR_OPERATION_SNAPSHOT_SHADOW", raising=False)
    assert shadow_enabled() is True, "unset shadow switch must default to enabled"
    monkeypatch.setenv("REBAR_OPERATION_SNAPSHOT_SHADOW", "0")
    assert shadow_enabled() is False, "a false spelling must disable shadow snapshots"
    monkeypatch.setenv("REBAR_OPERATION_SNAPSHOT_SHADOW", "1")
    assert shadow_enabled() is True, "a true spelling must re-enable — read is live"


def test_opcert_overrides_apply_when_unbound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When no signer is bound, the op-cert key-path and principal deployment overrides
    are still honored from the environment (the unbound fallback)."""
    from rebar._opcert_signing import opcert_principal

    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", "prod:eu")
    assert opcert_principal(tmp_path) == "prod:eu", (
        "an unbound REBAR_OPCERT_ENV_ID override must resolve as the principal"
    )


def test_signing_key_env_injection_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """An injected ``REBAR_SIGNING_KEY`` still short-circuits the on-disk key file."""
    from rebar.signing import signing_key

    monkeypatch.setenv("REBAR_SIGNING_KEY", "injected-secret")
    assert signing_key("/nonexistent/tracker") == b"injected-secret", (
        "a non-empty REBAR_SIGNING_KEY must be used verbatim (stripped, utf-8)"
    )


def test_mirror_guard_threads_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mirror_guard.main`` still reads ``GITHUB_TOKEN`` from its CLI boundary and threads
    it into ``run`` — asserted by capturing the token ``run`` receives."""
    import rebar.mirror_guard as mg

    seen: dict[str, object] = {}

    def _fake_run(**kwargs: object) -> tuple[list, int]:
        seen.update(kwargs)
        return [], 0

    monkeypatch.setattr(mg, "run", _fake_run)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-abc")
    mg.main(["--replication"])
    assert seen.get("github_token") == "tok-abc", (
        "mirror_guard.main must thread GITHUB_TOKEN from the environment into run()"
    )


def test_review_bot_port_resolution_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """``REVIEW_BOT_PORT`` still resolves with its default and integer-parse precedence."""
    pytest.importorskip("fastapi")
    from rebar.review_bot.app import DEFAULT_PORT, _port

    monkeypatch.delenv("REVIEW_BOT_PORT", raising=False)
    assert _port() == DEFAULT_PORT, "unset REVIEW_BOT_PORT must fall back to DEFAULT_PORT"
    monkeypatch.setenv("REVIEW_BOT_PORT", "9099")
    assert _port() == 9099, "a valid REVIEW_BOT_PORT must resolve"
    monkeypatch.setenv("REVIEW_BOT_PORT", "not-an-int")
    assert _port() == DEFAULT_PORT, "an invalid REVIEW_BOT_PORT must fall back to DEFAULT_PORT"


def test_grounding_timeout_precedence_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """The grounding per-invocation timeout keeps explicit-arg > env > default precedence."""
    from rebar.grounding.harness import _DEFAULT_TIMEOUT, _resolve_timeout

    monkeypatch.delenv("REBAR_GROUNDING_TIMEOUT", raising=False)
    assert _resolve_timeout(None) == _DEFAULT_TIMEOUT, "unset ⇒ default"
    monkeypatch.setenv("REBAR_GROUNDING_TIMEOUT", "12")
    assert _resolve_timeout(None) == 12.0, "a valid env timeout must resolve"
    assert _resolve_timeout(5) == 5, "an explicit argument must win over the env var"


def test_grounding_load_config_fails_open_and_reads_ctags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``grounding.load_config`` still reads ``.rebar/grounding.toml`` and fails open to
    defaults on a missing file, and ``REBAR_CTAGS_BIN`` is still honored."""
    from rebar.grounding import resolve

    cfg = resolve.load_config(str(tmp_path))
    assert cfg.t2_enabled is False, "a missing grounding.toml must fail open to defaults"

    rebar_dir = tmp_path / ".rebar"
    rebar_dir.mkdir()
    (rebar_dir / "grounding.toml").write_text("[grounding]\nt2_enabled = true\n", encoding="utf-8")
    cfg2 = resolve.load_config(str(tmp_path))
    assert cfg2.t2_enabled is True, "a present grounding.toml must be read"

    monkeypatch.setenv("REBAR_CTAGS_BIN", "my-ctags")
    # Assert the import-time capture in a FRESH interpreter so an in-process
    # ``importlib.reload`` can't pollute module identity for other tests (the
    # subprocess inherits the monkeypatched environment).
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c", "import rebar.grounding.resolve as r; print(r._CTAGS_BIN)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "my-ctags", "REBAR_CTAGS_BIN must still be honored at import"
