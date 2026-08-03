"""MUTATION CHECK for J11's rewritten inbound-assignee oracle (ticket 5200).

The live cell ``test_the_inbound_assignee_mints_a_jira_family_identity`` used to compare
``rebar.ensure_identity_for(...)``'s return value against itself. ``ensure_identity_for`` is
create-or-reuse, so that oracle MINTED the thing it was checking for and could not fail. It has
been rewritten to observe the identity REGISTRY across the pass, through the read-only
``rebar.resolve_mapping``.

An oracle rewritten for discriminating power has to be shown to HAVE it, and the live cell cannot
demonstrate that — a green live run is consistent with a tautology. So the halves are pinned
HERE, harness-free:

  * ``resolve_mapping`` returns None on a store nothing has minted into, so the rewritten
    oracle's ``minted is not None`` assertion genuinely FIRES when the pass mints nothing;
  * driving the PRODUCTION mint (``apply_inbound_records._ensure_inbound_assignee_identity``,
    the function the inbound apply actually calls) makes it resolve — so the oracle can also
    legitimately pass, and passes for the mint rather than for its own side effect;
  * the cell's SETUP step — ``_dc_support.forget_identity_mapping`` — really does re-establish
    the absence the oracle asserts, even on a store where the mapping was already minted. That
    is the shape the J11 harness (ticket 5200-e04e-246e-4aae) forced: the mapping is not
    left by the scrub (every
    identity on the real ``tickets`` branch carries ``mappings: []``) but minted DURING the test
    by ``bound_dc_issue``'s binding pass importing the seeded issue's default assignee. Without
    a working removal the cell can only ever fail at setup;
  * and the MUTATION: with the mint neutered, the oracle goes RED naming the missing identity.

The oracle's registry half is imported from ``_dc_support`` and run VERBATIM here rather than
paraphrased — a paraphrase can stay red while the live cell has quietly gone vacuous.

Deliberately in ``tests/unit/``: a module under ``tests/external/live_jira_dc/`` without a
harness skipif does not run harness-free — the autouse fixture burns the budget and then errors.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

import rebar
import rebar_reconciler.apply_inbound_records as air

_DC_USER = "admin"
_DC_USER_OBJECT = {"name": _DC_USER, "displayName": _DC_USER}


def _load_dc_support():
    """Import ``tests/external/live_jira_dc/_dc_support.py`` BY PATH.

    That directory is only on ``sys.path`` when pytest collects the external suite, so a
    plain ``import _dc_support`` works in a full run and fails in a unit-only one. Loading by
    path makes this module's dependency on the live suite's helpers explicit and order-free.
    ``_dc_support`` is not a ``test_*.py`` module, so importing it collects nothing.

    Its import builds ``skip_no_harness``, which PROBES the harness (``live_jira_ready`` opens
    ``$JIRA_DC_BASE_URL/rest/api/2/serverInfo``). Unit tests forbid network access, and rightly
    so, so the probe is stubbed to its unreachable answer for the duration of the import only —
    nothing here consults that marker.
    """
    path = Path(__file__).resolve().parents[2] / "external" / "live_jira_dc" / "_dc_support.py"
    spec = importlib.util.spec_from_file_location("_dc_support_for_5200_oracle", path)
    assert spec and spec.loader, f"could not load the live suite's helpers from {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    real_urlopen = urllib.request.urlopen

    def _no_probe(*_args, **_kwargs):
        raise OSError("harness probe suppressed: unit tests do not touch the network")

    urllib.request.urlopen = _no_probe  # type: ignore[assignment]
    try:
        spec.loader.exec_module(module)
    finally:
        urllib.request.urlopen = real_urlopen  # type: ignore[assignment]
    return module


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.com"),
        ("git", "config", "user.name", "d"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def test_the_oracle_can_go_red_because_resolve_mapping_never_creates(store: Path) -> None:
    """THE HALF THE OLD ORACLE LACKED. An unminted store resolves to None, so RED is reachable.

    This is the whole point of swapping ``ensure_identity_for`` for ``resolve_mapping``: the
    latter is a pure read. If it created on miss, the live oracle would be a tautology again and
    no live run could ever detect bug 5f48's silent swallow.
    """
    assert rebar.resolve_mapping("jira", _DC_USER, repo_root=store) is None, (
        "resolve_mapping returned an id for a user nothing has minted — it is not read-only, "
        "and the rewritten live oracle is therefore still unable to fail"
    )
    # Called twice on purpose: a lazily-creating implementation could return None once and an
    # id after, which would make the live oracle pass on its own second look.
    assert rebar.resolve_mapping("jira", _DC_USER, repo_root=store) is None, (
        "the second resolve_mapping returned an id, so the first call had a creating side effect"
    )


def test_the_oracle_goes_green_on_the_real_dc_mint(store: Path) -> None:
    """THE OTHER HALF. The PRODUCTION mint satisfies every clause the live oracle asserts.

    Driving ``_ensure_inbound_assignee_identity`` — not ``ensure_identity_for`` — is deliberate:
    it is the function the inbound apply calls, it takes the RAW Jira user object, and for Data
    Center that object has NO ``accountId``, only ``name`` (bug 5f48's exact shape). A test that
    called the registry directly would prove the oracle self-consistent while saying nothing
    about whether the DC path reaches it.

    The assertions are the LIVE CELL'S OWN, imported rather than restated.
    """
    air._ensure_inbound_assignee_identity(dict(_DC_USER_OBJECT), repo_root=str(store))

    minted = _load_dc_support().assert_mint_registered(store, _DC_USER)
    assert minted, "the oracle returned a falsy identity id while claiming the mint registered"


def test_the_cells_setup_step_really_re_establishes_the_absence(store: Path) -> None:
    """THE SETUP THE LIVE CELL NOW DOES FOR ITSELF, verified against a store that HAS the mapping.

    The J11 harness failed the precondition, and the reason matters: the mapping is
    minted during the run by ``bound_dc_issue``'s binding pass importing the seeded issue's
    default assignee, so no choice of subject avoids it. The cell removes the mapping instead.
    If that removal did not work the cell could only ever fail at setup — this is what proves it
    reaches its real assertions.
    """
    support = _load_dc_support()

    air._ensure_inbound_assignee_identity(dict(_DC_USER_OBJECT), repo_root=str(store))
    first = rebar.resolve_mapping("jira", _DC_USER, repo_root=store)
    assert first is not None, "setup precondition: the mint did not register, nothing to remove"

    removed = support.forget_identity_mapping(store, "jira", _DC_USER)

    assert removed == [first], (
        f"forget_identity_mapping removed {removed!r}, expected exactly the one carrier {first!r}"
    )
    assert rebar.resolve_mapping("jira", _DC_USER, repo_root=store) is None, (
        "the mapping still resolves after removal, so the live cell's setup assertion can never "
        "be satisfied and the cell is unpassable rather than merely red"
    )

    # AND THE PASS CAN MINT AGAIN AFTERWARDS — removal must not poison the registry. A NEW id
    # proves the post-pass resolve observes a fresh mint rather than the removed one lingering.
    air._ensure_inbound_assignee_identity(dict(_DC_USER_OBJECT), repo_root=str(store))
    second = support.assert_mint_registered(store, _DC_USER)
    assert second != first, (
        f"the re-mint returned the SAME id {second!r} as the removed identity, so the identity "
        f"was not really gone and the oracle's before/after difference proves nothing"
    )


def test_the_oracle_goes_red_when_the_mint_is_a_no_op(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE MUTATION. Neuter the mint; the oracle must fail, and fail NAMING the missing identity.

    ``_ensure_inbound_assignee_identity`` is already best-effort — it swallows its own failures
    (``apply_inbound_records.py:130-135``) — so "the mint silently did nothing" is a REACHABLE
    production state, not a contrived one. That is bug 5f48's exact signature.

    The verdict is checked on the MESSAGE, not merely on ``AssertionError``: an ImportError or
    AttributeError would also stop the cell, and would prove nothing about its power to detect
    a silent swallow.
    """
    support = _load_dc_support()
    monkeypatch.setattr(air, "_ensure_inbound_assignee_identity", lambda *a, **k: None)

    air._ensure_inbound_assignee_identity(dict(_DC_USER_OBJECT), repo_root=str(store))

    with pytest.raises(AssertionError) as excinfo:
        support.assert_mint_registered(store, _DC_USER)

    message = str(excinfo.value)
    assert "THE PASS MINTED NOTHING" in message and _DC_USER in message, (
        f"the oracle failed, but not for the missing identity — message was: {message!r}"
    )
