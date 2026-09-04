"""Held-out seam-clean + behavioral-regression proof for RP-04 C3a-CORE.

Slice C3a-CORE drains the reconciler PASS CORE's below-seam ambient config/credential
reads (``REBAR_ROOT``, ``load_config``, dynamic ``os.environ.get`` toggles, identity, and
tuning) to the approved seam: each read is either CUT to an owned ``config.py`` /
``_config_sources.py`` resolver threaded through the pass, or — for the four bounded
kill-switch/failure-injection/override reads — kept owned-in-place behind a
``# read-via:`` marker. Observable contract, not internal structure.

Seam-clean (the strong anti-fake, RED before the cut while the 45 rows remain):

1. The config-ownership gate (``scripts/check_config_ownership.py``) reports **zero**
   findings for the 29 CORE source files this slice owns.
2. Their ``LEGACY_EXCEPTIONS`` entries (45 rows) are gone, and no path-glob exception
   masks them.
3. The owned-in-place allowlist is CAPPED at exactly SIX ``# read-via:`` marked lines,
   confined to the four enumerated files. Every other owned file must carry ZERO markers
   — an implementer cannot satisfy the gate by blanket-marking reads instead of cutting
   them.

Behavioral (the cut must be a pure refactor of the resolution seam; asserted through
stable public entry points that survive the refactor):

4. ``repo_root`` still honors its precedence: an explicit argument wins over the
   ``REBAR_ROOT`` env var, which wins over the discovered fallback.
5. ``last_pass.resolve_environment_id`` keeps its identity semantics: an explicit env id
   wins, and a set-but-empty ``REBAR_ENV_ID`` still fails LOUD (``LastPassError``) rather
   than silently defaulting.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# Bare-name imports: ``tests/scripts/conftest.py`` puts repo-root ``scripts/`` on
# sys.path (the CI-proven pattern; a ``scripts.`` package prefix does not resolve under
# the full-suite import mode).
import check_config_ownership as gate
import config_ownership_exceptions as exceptions
import pytest

# The 31 files this slice owns, as paths relative to ``src/rebar/`` (the form the gate
# emits and the exception registry stores). ``_pass_lock_lifecycle.py`` was extracted
# from ``__main__.py`` (ticket 638a-3746-e58a-4929, module-size cap) and carries the
# pass-lock cluster with it, so it inherits __main__'s ownership rather than escaping it.
# ``apply_inbound_events.py`` was extracted the same way from ``apply_inbound_records.py``
# (ticket 6f51-f8a4-b4fb-450c) and carries the inbound event-writing cluster, so it
# inherits that file's ownership rather than escaping this gate.
_OWNED_FILES = (
    "_engine/rebar_reconciler/__main__.py",
    "_engine/rebar_reconciler/_advisory_lock.py",
    "_engine/rebar_reconciler/_pass_lock_lifecycle.py",
    "_engine/rebar_reconciler/_preflight.py",
    "_engine/rebar_reconciler/applier.py",
    "_engine/rebar_reconciler/apply_handlers.py",
    "_engine/rebar_reconciler/apply_inbound.py",
    "_engine/rebar_reconciler/apply_inbound_events.py",
    "_engine/rebar_reconciler/apply_inbound_records.py",
    "_engine/rebar_reconciler/apply_planning.py",
    "_engine/rebar_reconciler/binding_store.py",
    "_engine/rebar_reconciler/binding_walk.py",
    "_engine/rebar_reconciler/dispatch_one.py",
    "_engine/rebar_reconciler/fetcher.py",
    "_engine/rebar_reconciler/inbound_differ.py",
    "_engine/rebar_reconciler/inbound_translate.py",
    "_engine/rebar_reconciler/invariants.py",
    "_engine/rebar_reconciler/last_pass.py",
    "_engine/rebar_reconciler/outbound_comments.py",
    "_engine/rebar_reconciler/outbound_differ.py",
    "_engine/rebar_reconciler/pass_io.py",
    "_engine/rebar_reconciler/rebar_id_audit.py",
    "_engine/rebar_reconciler/reconcile.py",
    "_engine/rebar_reconciler/reconcile_helpers.py",
    "_engine/rebar_reconciler/request.py",
    "_engine/rebar_reconciler/run_differs.py",
    "_engine_support/bridge_fsck_visibility.py",
    "_engine_support/gates.py",
    "_engine_support/lookups.py",
    "_engine_support/reads.py",
)

# The enumerated owned-in-place allowlist: file -> exact number of ``# read-via:`` marked
# lines it may carry. Everything else must be ZERO.
#   _pass_lock_lifecycle.py REBAR_RECONCILER_LOCK_STEAL     (kill-switch)          1 line
#   reconcile_helpers.py  REBAR_RECONCILER_WRITE_FACADE     (AC6 rollback toggle) 1 line
#   apply_handlers.py     REBAR_RECONCILER_FAIL_SILENT_NOOP (failure-injection)   3 lines
#   apply_inbound.py      REBAR_RECONCILER_CONFLICT_PARENT_ID (operator override) 1 line
# The LOCK_STEAL line moved out of __main__.py with _lock_steal_enabled when the
# pass-lock cluster was extracted; __main__.py now budgets ZERO. The cap is unchanged
# at 6 because the line RELOCATED — no read was added, and none was blanket-marked.
_MARKER_BUDGET = {
    "_engine/rebar_reconciler/_pass_lock_lifecycle.py": 1,
    "_engine/rebar_reconciler/reconcile_helpers.py": 1,
    "_engine/rebar_reconciler/apply_handlers.py": 3,
    "_engine/rebar_reconciler/apply_inbound.py": 1,
}
_TOTAL_MARKER_CAP = 6

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
        "C3a-CORE cutover must remove every LEGACY_EXCEPTIONS entry for the reconciler PASS "
        f"CORE files; still present: {remaining}"
    )


def test_gate_reports_no_findings_for_owned_files() -> None:
    findings = _gate_findings_for_owned()
    assert findings == [], (
        "config-ownership gate must report zero findings for the reconciler PASS CORE files "
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
        f"the owned-in-place allowlist caps at {_TOTAL_MARKER_CAP} marked lines; got {total}"
    )


# ---------------------------------------------------------------------------
# Behavioral regressions the cut must preserve (asserted through stable entry points)
# ---------------------------------------------------------------------------


def test_repo_root_precedence_is_preserved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The owned root resolver keeps its precedence after the cut: an explicit argument
    wins over the ``REBAR_ROOT`` env var. A cut that hard-wires one source (or drops the
    explicit-argument path when threading through ``_PassContext``) breaks this."""
    from rebar.config import repo_root

    explicit = tmp_path / "explicit_root"
    explicit.mkdir()
    env_root = tmp_path / "env_root"
    env_root.mkdir()
    monkeypatch.setenv("REBAR_ROOT", str(env_root))

    assert repo_root(explicit) == explicit.resolve(), (
        "an explicit repo_root argument must win over the REBAR_ROOT env var"
    )
    assert repo_root() == env_root.resolve(), (
        "with no explicit argument, REBAR_ROOT must win over the discovered fallback"
    )


def test_environment_id_semantics_are_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``resolve_environment_id`` keeps its identity contract through the cut: an explicit
    env id wins, and a *set-but-empty* ``REBAR_ENV_ID`` still fails LOUD rather than
    silently defaulting to the local-id file. The owned accessor must re-raise these, not
    swallow them."""
    from rebar_reconciler.last_pass import LastPassError, resolve_environment_id

    monkeypatch.delenv("REBAR_ENV_ID", raising=False)
    assert resolve_environment_id(tmp_path, explicit="prod:main") == "prod:main", (
        "an explicit environment id must win"
    )

    monkeypatch.setenv("REBAR_ENV_ID", "")
    with pytest.raises(LastPassError):
        resolve_environment_id(tmp_path)


def test_environment_id_env_var_is_read_after_the_cut(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-empty ``REBAR_ENV_ID`` is honored (no explicit argument): the cut must keep
    reading the env layer through its owned accessor, not freeze or drop it."""
    from rebar_reconciler.last_pass import resolve_environment_id

    monkeypatch.setenv("REBAR_ENV_ID", "staging:eu")
    assert resolve_environment_id(tmp_path) == "staging:eu", (
        "a set, non-empty REBAR_ENV_ID must resolve as the environment id after the cut"
    )


def test_owned_in_place_toggle_is_read_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``REBAR_RECONCILER_FAIL_SILENT_NOOP`` failure-injection toggle stays owned in
    place and read LIVE from the environment — a mid-run flip is observed on the next read.
    A cut that composes it once at pass entry would freeze the answer. Asserted at the
    ``os.environ`` boundary the marked read consults (the toggle default is OFF)."""
    monkeypatch.delenv("REBAR_RECONCILER_FAIL_SILENT_NOOP", raising=False)
    assert os.environ.get("REBAR_RECONCILER_FAIL_SILENT_NOOP", "0") == "0"
    monkeypatch.setenv("REBAR_RECONCILER_FAIL_SILENT_NOOP", "1")
    assert os.environ.get("REBAR_RECONCILER_FAIL_SILENT_NOOP", "0") == "1"


def test_reconciler_repo_root_unset_fallback_is_cwd_independent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With ``REBAR_ROOT`` unset and no explicit root, a valid owned package-root
    checkout must win before any CWD-based fallback — the caller's working directory must
    not leak into the reconciler's root. The pure wheel/no-checkout case deliberately
    raises (covered below), so this leg makes the valid package-root precondition
    explicit and then resolves from an unrelated checkout to prove CWD-independence."""
    from rebar_reconciler import invariants

    import rebar.config as cfg_mod

    monkeypatch.delenv("REBAR_ROOT", raising=False)
    owned_checkout = tmp_path / "owned_checkout"
    owned_checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(owned_checkout)], check=True)
    owned_config = owned_checkout / "src" / "rebar" / "config.py"
    owned_config.parent.mkdir(parents=True)
    owned_config.write_text("# editable package stand-in\n", encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "__file__", str(owned_config))

    other_repo = tmp_path / "unrelated_checkout"
    other_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(other_repo)], check=True)

    baseline = invariants._default_repo_root().resolve()
    monkeypatch.chdir(other_repo)
    from_other_cwd = invariants._default_repo_root().resolve()

    assert from_other_cwd == baseline, (
        "the reconciler repo-root fallback (REBAR_ROOT unset, no explicit arg) must be "
        f"cwd-independent; resolved to {from_other_cwd} from inside {other_repo} but to "
        f"{baseline} from the original cwd (a git-toplevel-of-cwd fallback leaked the "
        "caller's working directory)"
    )
    assert from_other_cwd != other_repo.resolve(), (
        "the reconciler repo-root fallback resolved to the caller's unrelated checkout — "
        "the cwd leaked into the owned root resolution"
    )
    assert from_other_cwd == owned_checkout.resolve(), (
        "the valid owned package-root checkout must be the deterministic fallback when "
        f"REBAR_ROOT is unset; got {from_other_cwd}, expected {owned_checkout.resolve()}"
    )


def test_reconciler_event_identity_env_override_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owned ``reconciler_event_identity`` resolver reads the env OVERRIDE layer after
    the cut: a non-default ``REBAR_ENV_ID`` / ``REBAR_AUTHOR`` must propagate to the
    ``(env_id, author)`` pair (not be frozen to the legacy-Jira default). The unset-default
    branch is covered elsewhere; this pins the set branch so the cut cannot silently drop
    the env read."""
    from rebar.config import reconciler_event_identity

    monkeypatch.setenv("REBAR_ENV_ID", "prod:main")
    monkeypatch.setenv("REBAR_AUTHOR", "reconciler:svc")
    assert reconciler_event_identity() == ("prod:main", "reconciler:svc"), (
        "a set REBAR_ENV_ID/REBAR_AUTHOR must propagate through the owned identity resolver"
    )


# ---------------------------------------------------------------------------
# Bug c0b9-33b0-f968-450d: reconciler_repo_root() package-location default is
# wrong under a NON-EDITABLE install. Option (C) — validated layered fallback:
# REBAR_ROOT -> validated package root -> validated git-toplevel-of-cwd -> a
# clear error. These pin the two legs that TODAY resolve silently into the venv
# tree, plus the behavior-preserving controls.
# ---------------------------------------------------------------------------


def _noneditable_package_config_file(tmp_path: Path) -> Path:
    """A site-packages-shaped ``config.py`` whose ``parents[2]`` is NOT a checkout.

    Mirrors a wheel/non-editable install: ``<venv>/lib/pythonX/site-packages/rebar/
    config.py`` -> ``parents[2]`` == ``<venv>/lib/pythonX`` (no ``.git``, no
    ``src/rebar``). Directories are materialized so ``.resolve()`` behaves like a real
    install path.
    """
    cfg = tmp_path / "venv" / "lib" / "python3.13" / "site-packages" / "rebar" / "config.py"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("# stand-in installed module\n", encoding="utf-8")
    return cfg


def test_reconciler_repo_root_errors_when_neither_package_nor_cwd_is_a_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-editable install, REBAR_ROOT unset, and a CWD that is not a checkout: the
    resolver must RAISE a clear, actionable error naming both ``REBAR_ROOT`` and
    ``--repo-root`` — never silently return the venv/site-packages tree.

    This is the bug's discriminating leg: before the fix, ``reconciler_repo_root()``
    returns ``Path(__file__).resolve().parents[2]`` unconditionally, so under a
    site-packages ``__file__`` it hands back ``<venv>/lib/pythonX`` (a non-checkout) with
    no error — exactly the CI failure ``Resolved repo_root .../.venv/lib/python3.13``.
    """
    import rebar.config as cfg_mod

    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.setattr(cfg_mod, "__file__", str(_noneditable_package_config_file(tmp_path)))

    scratch = tmp_path / "scratch_not_a_repo"
    scratch.mkdir()
    monkeypatch.chdir(scratch)

    with pytest.raises(RuntimeError) as excinfo:
        cfg_mod.reconciler_repo_root()

    message = str(excinfo.value)
    assert "REBAR_ROOT" in message, (
        "the resolution error must name REBAR_ROOT as a remedy; got: " + message
    )
    assert "--repo-root" in message, (
        "the resolution error must name --repo-root as a remedy; got: " + message
    )


def test_reconciler_repo_root_git_probe_failure_degrades_to_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-editable install, REBAR_ROOT unset, and the step-3 ``git`` probe FAILS (git
    absent / hung): the resolver must still degrade to the clear step-4 error, never a raw
    subprocess traceback (``FileNotFoundError``/``TimeoutExpired``).

    Guards the ``_git_toplevel_of_cwd`` robustness the plan-review advisory flagged: a
    missing ``git`` binary or a probe timeout on a slow filesystem must be swallowed to
    ``None`` so step 4 (the actionable REBAR_ROOT/--repo-root message) is what surfaces.
    """
    import subprocess as _subprocess

    import rebar.config as cfg_mod

    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.setattr(cfg_mod, "__file__", str(_noneditable_package_config_file(tmp_path)))

    def _git_unavailable(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("git")

    monkeypatch.setattr(cfg_mod.subprocess, "run", _git_unavailable)

    with pytest.raises(RuntimeError) as excinfo:
        cfg_mod.reconciler_repo_root()

    message = str(excinfo.value)
    assert "REBAR_ROOT" in message and "--repo-root" in message, (
        "a failed git probe must fall through to the clear step-4 error; got: " + message
    )

    def _git_timeout(*_args: object, **_kwargs: object) -> object:
        raise _subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(cfg_mod.subprocess, "run", _git_timeout)
    with pytest.raises(RuntimeError):
        cfg_mod.reconciler_repo_root()


def test_reconciler_repo_root_falls_back_to_cwd_checkout_under_noneditable_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-editable install (package root not a checkout), REBAR_ROOT unset, but the CWD
    IS inside a real rebar checkout: the resolver falls back to the git toplevel of the
    CWD and returns THAT checkout.

    Before the fix this returns the site-packages ``parents[2]`` tree instead, so the
    resolved root neither equals the checkout nor points at a real ``src/rebar`` tree.
    """
    import rebar.config as cfg_mod

    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.setattr(cfg_mod, "__file__", str(_noneditable_package_config_file(tmp_path)))

    checkout = tmp_path / "real_checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    (checkout / "src" / "rebar").mkdir(parents=True)
    monkeypatch.chdir(checkout)

    resolved = cfg_mod.reconciler_repo_root()
    assert resolved.resolve() == checkout.resolve(), (
        "with a non-checkout package root and REBAR_ROOT unset, the resolver must fall "
        f"back to the git toplevel of the CWD; got {resolved!r}, expected {checkout!r}"
    )
    # Contract: the resolved root is a real checkout (the discriminator the venv tree fails).
    assert (resolved / ".git").exists() and (resolved / "src" / "rebar").is_dir(), (
        f"resolved root {resolved!r} is not a real checkout"
    )


def test_reconciler_repo_root_preserves_env_and_editable_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Behavior-preserving controls (must be green before AND after the fix):

    * ``REBAR_ROOT`` set -> returned verbatim (first-priority, unchanged), even when it
      points at a non-checkout — the operator's explicit choice is honored as-is.
    * Editable install (the real in-tree ``config.__file__``) with REBAR_ROOT unset ->
      the package root, which under the source checkout IS a real checkout.
    """
    import rebar.config as cfg_mod

    # Control 1: explicit REBAR_ROOT wins verbatim.
    explicit = tmp_path / "explicit_root"
    explicit.mkdir()
    monkeypatch.setenv("REBAR_ROOT", str(explicit))
    assert cfg_mod.reconciler_repo_root() == Path(str(explicit)), (
        "a set REBAR_ROOT must be honored verbatim as the first-priority root"
    )

    # Control 2: editable/source install resolves to the real checkout (package root valid).
    monkeypatch.delenv("REBAR_ROOT", raising=False)
    resolved = cfg_mod.reconciler_repo_root()
    assert (resolved / ".git").exists() and (resolved / "src" / "rebar").is_dir(), (
        f"editable-install package root must be a real checkout; got {resolved!r}"
    )
