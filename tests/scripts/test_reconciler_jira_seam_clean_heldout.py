"""Held-out seam-clean + behavioral-regression proof for RP-04 C3a-JIRA.

Slice C3a-JIRA cuts the reconciler JIRA adapter/credential surface's below-seam ambient
config reads to the approved seam (routed through an owned ``config.py`` resolver) or
annotates the single bounded owned-in-place env-control read with a ``# read-via:``
marker. Observable contract, not internal structure. Four seam-clean properties plus the
behavioral regressions the cut must preserve.

Seam-clean:

1. The config-ownership gate (``scripts/check_config_ownership.py``) reports **zero**
   findings for the 6 source files this slice owns.
2. Their ``LEGACY_EXCEPTIONS`` entries (12 rows) are gone, and no path-glob exception
   masks them.
3. The owned-in-place allowlist is CAPPED at exactly ONE logical read = one
   ``# read-via:`` marked line, confined to ``adapters/jira/outbound_fields.py``
   (``REBAR_RECONCILER_VERBOSE``). Every other owned file must carry ZERO markers — an
   implementer cannot satisfy the gate by blanket-marking reads instead of cutting them.

Behavioral (the cut must be a pure refactor of the resolution seam):

4. **JIRA endpoint resolves LIVE per call** through the env-fallback path, and a
   deliberately-insecure ``jira.url`` still FAILS LOUD (``InsecureUrlError`` propagates).
5. **The stdlib-only-subprocess importability invariant holds** — ``cutover_clients`` and
   ``_pandoc_timeout`` run inside a pandoc subprocess where ``rebar`` may be unimportable,
   so a lazy ``import rebar...`` that raises ``ImportError`` at call time must DEGRADE to
   the safe default, never propagate.
6. **``reconciler.rich_text_cutover`` is read live per call**, so flipping the flag needs
   no redeploy and a compose-once cut cannot freeze the answer.

RED before the cutover: the gate/legacy-row assertions fail while the 12 rows remain.
"""

from __future__ import annotations

import builtins
import contextlib
import re

# Bare-name imports: ``tests/scripts/conftest.py`` puts repo-root ``scripts/`` on
# sys.path (the CI-proven pattern; a ``scripts.`` package prefix does not resolve under
# the full-suite import mode).
import check_config_ownership as gate
import config_ownership_exceptions as exceptions
import pytest

# The 6 files this slice owns, as paths relative to ``src/rebar/`` (the form the gate
# emits and the exception registry stores).
_OWNED_FILES = (
    "_engine/rebar_reconciler/access_check.py",
    "_engine/rebar_reconciler/adapters/jira/acli_subprocess.py",
    "_engine/rebar_reconciler/adapters/jira/outbound_fields.py",
    "_engine/rebar_reconciler/adapters/jira_datacenter/settings.py",
    "_engine/rebar_reconciler/adapters/jira_family/rich_text.py",
    "_engine/rebar_reconciler/adapters/jira_family/wiki_render.py",
)

# The enumerated owned-in-place allowlist: file -> exact number of ``# read-via:`` marked
# lines it may carry. Everything else must be ZERO.
_MARKER_BUDGET = {
    "_engine/rebar_reconciler/adapters/jira/outbound_fields.py": 1,  # REBAR_RECONCILER_VERBOSE
}
_TOTAL_MARKER_CAP = 1

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
        "C3a-JIRA cutover must remove every LEGACY_EXCEPTIONS entry for the reconciler "
        f"JIRA files; still present: {remaining}"
    )


def test_gate_reports_no_findings_for_owned_files() -> None:
    findings = _gate_findings_for_owned()
    assert findings == [], (
        "config-ownership gate must report zero findings for the reconciler JIRA files "
        f"after the cutover; got: {findings}"
    )


def test_no_path_glob_exception_masks_the_owned_files() -> None:
    globbed = [
        e["path"]
        for e in exceptions.LEGACY_EXCEPTIONS
        if any(ch in str(e["path"]) for ch in "*?[]")
        and any(name in str(e["path"]) for name in _OWNED_FILES)
    ]
    assert globbed == [], f"no path-glob exception may mask the owned files; got: {globbed}"


def test_read_via_markers_are_bounded_and_confined() -> None:
    per_file = {rel: _marker_count(rel) for rel in _OWNED_FILES}
    unexpected = {rel: n for rel, n in per_file.items() if n and rel not in _MARKER_BUDGET}
    assert unexpected == {}, (
        "owned-in-place markers are confined to the enumerated allowlist; unexpected "
        f"markers in: {unexpected}"
    )
    wrong = {
        rel: (per_file[rel], want) for rel, want in _MARKER_BUDGET.items() if per_file[rel] != want
    }
    assert wrong == {}, (
        "each allowlisted file must carry exactly its marked-line budget "
        f"(got (actual, want)): {wrong}"
    )
    total = sum(per_file.values())
    assert total == _TOTAL_MARKER_CAP, (
        f"the owned-in-place allowlist caps at {_TOTAL_MARKER_CAP} marked line; got {total}"
    )


# ---------------------------------------------------------------------------
# Behavioral regressions the cut must preserve (asserted through stable entry points)
# ---------------------------------------------------------------------------


def test_jira_settings_env_fallback_is_read_live_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the typed config is unreadable the endpoint degrades to the JIRA_* env layer,
    and it re-reads that layer LIVE per call — a mid-pass override is observed on the next
    call. A compose-once cut would cache the first value."""
    from rebar_reconciler.adapters.jira.acli_subprocess import resolve_jira_settings

    import rebar.config

    def _raise_config_error(*_a, **_k):
        raise rebar.config.ConfigError("unreadable")

    monkeypatch.setattr(rebar.config, "load_config", _raise_config_error)

    monkeypatch.setenv("JIRA_URL", "https://one.example")
    monkeypatch.setenv("JIRA_USER", "u1")
    monkeypatch.setenv("JIRA_PROJECT", "P1")
    first = resolve_jira_settings()
    assert (first.url, first.user, first.project) == ("https://one.example", "u1", "P1")

    monkeypatch.setenv("JIRA_URL", "https://two.example")
    monkeypatch.setenv("JIRA_PROJECT", "P2")
    second = resolve_jira_settings()
    assert (second.url, second.project) == ("https://two.example", "P2"), (
        "resolve_jira_settings must re-read the JIRA_* env layer live per call after the cut"
    )


def test_jira_settings_insecure_url_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cleartext ``jira.url`` is a deliberate security-policy rejection: the cut must keep
    it FAILING LOUD (``InsecureUrlError`` propagates), not degrade to env like a malformed
    config does."""
    from rebar_reconciler.adapters.jira.acli_subprocess import resolve_jira_settings

    import rebar.config

    def _raise_insecure(*_a, **_k):
        raise rebar.config.InsecureUrlError("cleartext jira.url")

    monkeypatch.setattr(rebar.config, "load_config", _raise_insecure)
    with pytest.raises(rebar.config.InsecureUrlError):
        resolve_jira_settings()


@contextlib.contextmanager
def _rebar_unimportable():
    """Make a call-time ``import rebar.<...>`` raise ImportError for the duration of the
    ``with`` block only, simulating the stdlib-only pandoc subprocess where ``rebar`` is
    not on the path. Scoped tightly (restored before any fixture teardown) and leaves
    ``rebar_reconciler`` — a distinct top-level name — untouched, so already-imported
    adapter modules keep working."""
    real_import = builtins.__import__

    def _fake(name, *args, **kwargs):
        if name == "rebar" or name.startswith("rebar."):
            raise ImportError(f"blocked: {name}")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _fake
    try:
        yield
    finally:
        builtins.__import__ = real_import


def test_cutover_clients_degrades_when_rebar_unimportable() -> None:
    from rebar_reconciler.adapters.jira_family.rich_text import cutover_clients

    with _rebar_unimportable():
        result = cutover_clients()
    assert result == frozenset(), (
        "cutover_clients must fail CLOSED to the empty set (plain wire) when rebar is not "
        "importable at call time — the delegating cut must keep the ImportError->default guard"
    )


def test_pandoc_timeout_degrades_when_rebar_unimportable() -> None:
    from rebar_reconciler.adapters.jira_family import wiki_render

    with _rebar_unimportable():
        result = wiki_render._pandoc_timeout()
    assert result == wiki_render._PANDOC_TIMEOUT_DEFAULT and result > 0, (
        "_pandoc_timeout must fall back to the positive built-in default when rebar is not "
        "importable at call time — the delegating cut must keep the ImportError->default guard"
    )


def test_cutover_clients_read_live_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flipping ``reconciler.rich_text_cutover`` between calls is observed immediately: the
    cut resolver must read the flag live per call, never freeze it at compose time."""
    from rebar_reconciler.adapters.jira_family.rich_text import cutover_clients

    import rebar.config

    class _Cfg:
        def __init__(self, value: str) -> None:
            self.reconciler = type("R", (), {"rich_text_cutover": value})()

    state = {"value": "off"}
    monkeypatch.setattr(rebar.config, "load_config", lambda *a, **k: _Cfg(state["value"]))

    assert cutover_clients() == frozenset()
    state["value"] = "both"
    assert cutover_clients() == frozenset({"cloud", "dc"}), (
        "cutover_clients must re-read reconciler.rich_text_cutover live per call after the cut"
    )
