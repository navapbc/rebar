"""The remediation-mode eligibility GATE (epic 7d43, child ec89).

The freshness-window + precondition check that decides whether a re-review is eligible for
remediation mode (the Pass-3 rising floor it gates is child cc5b). These tests pin: ALL
preconditions are required (any one failing → not eligible → full review), the code-unchanged /
plan-changed / registry-unchanged signals, the window math (reset on each review), the
default-ON config (since 2026-07-11), and the byte-identical full-review back-out (explicit
false).
"""

from __future__ import annotations

import pytest

import rebar.signing as signing
from rebar import config as core_config
from rebar.llm import config as llm_config
from rebar.llm.plan_review import attest, sidecar

pytestmark = pytest.mark.unit

_WINDOW = 60
_MIN_NS = 60 * 1_000_000_000
_DEFAULT = object()  # sentinel: "use the default prior findings" (distinct from None = no sidecar)


def _manifest(material: str, regver: str, sha: str | None) -> list[str]:
    return attest.build_manifest(
        {"verdict": "PASS", "ticket_id": "T", "model": "m", "runner": "r"},
        material=material,
        regver=regver,
        verified_at_sha=sha,
    )


def _setup(
    monkeypatch,
    *,
    signed: bool = True,
    prior_material: str = "OLD",
    cur_material: str = "NEW",
    prior_sha: str | None = "sha-baseline",
    cur_sha: str | None = "sha-baseline",
    prior_regver: str = "REG1",
    cur_regver: str = "REG1",
    prior_findings=_DEFAULT,
    last_ts: int | None = 1_000 * _MIN_NS,
    sidecar_baseline: bool = True,
) -> None:
    if prior_findings is _DEFAULT:
        prior_findings = [{"finding": "the prior defect"}]
    manifest = _manifest(prior_material, prior_regver, prior_sha) if signed else None
    monkeypatch.setattr(
        signing,
        "verify_signature",
        lambda tid, repo_root=None: {"verified": signed, "manifest": manifest, "key_id": "k"},
    )
    monkeypatch.setattr(
        attest, "current_material_fingerprint", lambda tid, repo_root=None: cur_material
    )
    monkeypatch.setattr(llm_config, "current_code_sha", lambda: cur_sha)
    monkeypatch.setattr(attest, "registry_version", lambda repo_root=None: cur_regver)
    monkeypatch.setattr(
        sidecar,
        "latest_review_result",
        lambda tid, repo_root=None: (
            {
                "findings": prior_findings,
                # story a850: every sidecar stamps the eligibility baseline; tests control
                # presence via sidecar_baseline (None values = a pre-a850 / failed-stamp payload).
                "material_fingerprint": prior_material if sidecar_baseline else None,
                "verified_at_sha": prior_sha if sidecar_baseline else None,
                "regver": prior_regver if sidecar_baseline else None,
            }
            if prior_findings is not None
            else None
        ),
    )
    monkeypatch.setattr(sidecar, "latest_review_timestamp", lambda tid, repo_root=None: last_ts)
    # the sidecar branch resolves its CURRENT side through review_code_sha (one rule, both sides)
    monkeypatch.setattr(sidecar, "review_code_sha", lambda repo_root=None: cur_sha)


def _decide(now_ns: int = 1_000 * _MIN_NS + 5 * _MIN_NS) -> dict:
    # default now = 5 minutes after the last review (well within the 60-min window)
    return attest.remediation_mode_candidate("T", window_minutes=_WINDOW, now_ns=now_ns)


def test_all_preconditions_met_is_eligible(monkeypatch) -> None:
    _setup(monkeypatch)
    d = _decide()
    assert d["eligible"] is True
    assert all(d["reasons"].values())


def test_no_signature_no_sidecar_baseline_not_eligible(monkeypatch) -> None:
    """No signature AND no usable sidecar baseline (a pre-a850 payload without the stamps) →
    ineligible, decided by the SIDECAR branch (no `signed` key there)."""
    _setup(monkeypatch, signed=False, sidecar_baseline=False)
    d = _decide()
    assert d["eligible"] is False
    assert d["baseline"] == "sidecar"
    assert d["reasons"]["sidecar_baseline"] is False
    assert "signed" not in d["reasons"]


def test_no_signature_with_sidecar_baseline_is_eligible(monkeypatch) -> None:
    """The a850 headline case: a BLOCK loop (no signature ever minted) with a stamped prior
    sidecar, plan changed, code + registry unchanged, within window → ELIGIBLE."""
    _setup(monkeypatch, signed=False)
    d = _decide()
    assert d["eligible"] is True
    assert d["baseline"] == "sidecar"
    assert set(d["reasons"]) == {
        "sidecar_baseline",
        "plan_changed",
        "code_unchanged",
        "registry_unchanged",
        "within_window",
    }


def test_sidecar_branch_code_drift_not_eligible(monkeypatch) -> None:
    _setup(monkeypatch, signed=False, cur_sha="sha-drifted")
    d = _decide()
    assert d["eligible"] is False
    assert d["reasons"]["code_unchanged"] is False


def test_sidecar_branch_registry_skew_not_eligible(monkeypatch) -> None:
    _setup(monkeypatch, signed=False, cur_regver="REG2")
    d = _decide()
    assert d["eligible"] is False
    assert d["reasons"]["registry_unchanged"] is False


def test_signature_branch_unchanged_and_tagged(monkeypatch) -> None:
    """A valid signature keeps today's decision shape verbatim (plus the baseline tag) —
    the sidecar fallback is not consulted."""
    _setup(monkeypatch)
    d = _decide()
    assert d["eligible"] is True
    assert d["baseline"] == "signature"
    assert set(d["reasons"]) == {
        "signed",
        "plan_changed",
        "code_unchanged",
        "registry_unchanged",
        "prior_sidecar",
        "within_window",
    }


def test_plan_unchanged_not_eligible(monkeypatch) -> None:
    """Same material as the prior signed review → nothing to re-review under the floor."""
    _setup(monkeypatch, prior_material="SAME", cur_material="SAME")
    d = _decide()
    assert d["reasons"]["plan_changed"] is False
    assert d["eligible"] is False


def test_code_changed_not_eligible(monkeypatch) -> None:
    """Reviewed code drifted (verified_at_sha differs) → full review, not remediation."""
    _setup(monkeypatch, prior_sha="sha-old", cur_sha="sha-new")
    d = _decide()
    assert d["reasons"]["code_unchanged"] is False
    assert d["eligible"] is False


def test_local_mode_sha_is_not_code_unchanged(monkeypatch) -> None:
    """A local-mode review (no verified_at_sha on either side) is NOT a reliable
    code-unchanged signal → treated as changed."""
    _setup(monkeypatch, prior_sha=None, cur_sha=None)
    d = _decide()
    assert d["reasons"]["code_unchanged"] is False
    assert d["eligible"] is False


def test_registry_skew_not_eligible(monkeypatch) -> None:
    _setup(monkeypatch, prior_regver="REG1", cur_regver="REG2")
    d = _decide()
    assert d["reasons"]["registry_unchanged"] is False
    assert d["eligible"] is False


def test_no_prior_sidecar_findings_not_eligible(monkeypatch) -> None:
    """A prior sidecar with no finding text (or none at all) → the novelty sub-call has no
    prior findings to ground on → not eligible."""
    _setup(monkeypatch, prior_findings=[{"finding": "   "}])  # blank text
    assert _decide()["reasons"]["prior_sidecar"] is False
    _setup(monkeypatch, prior_findings=None)  # no sidecar at all
    d = _decide()
    assert d["reasons"]["prior_sidecar"] is False
    assert d["eligible"] is False


def test_window_lapsed_not_eligible(monkeypatch) -> None:
    """Last review older than the window → the agent went idle → full (un-floored) review."""
    _setup(monkeypatch, last_ts=1_000 * _MIN_NS)
    # now = 61 minutes after the last review (> 60-min window)
    d = attest.remediation_mode_candidate(
        "T", window_minutes=_WINDOW, now_ns=1_000 * _MIN_NS + 61 * _MIN_NS
    )
    assert d["reasons"]["within_window"] is False
    assert d["eligible"] is False


def test_window_reset_on_each_review_keeps_loop_alive(monkeypatch) -> None:
    """The window is measured from the LAST review (newest sidecar), so a fresh review keeps
    the loop within the window even long after the first edit."""
    _setup(monkeypatch, last_ts=10_000 * _MIN_NS)  # a recent review
    d = attest.remediation_mode_candidate(
        "T", window_minutes=_WINDOW, now_ns=10_000 * _MIN_NS + 3 * _MIN_NS
    )
    assert d["reasons"]["within_window"] is True
    assert d["eligible"] is True


def test_no_prior_sidecar_timestamp_not_eligible(monkeypatch) -> None:
    _setup(monkeypatch, last_ts=None)
    d = _decide()
    assert d["reasons"]["within_window"] is False
    assert d["eligible"] is False


def test_candidate_never_raises_on_signing_error(monkeypatch) -> None:
    def boom(tid, repo_root=None):
        raise RuntimeError("signing down")

    monkeypatch.setattr(signing, "verify_signature", boom)
    d = attest.remediation_mode_candidate("T", window_minutes=_WINDOW, now_ns=_MIN_NS)
    assert d["eligible"] is False  # fail-safe → full review


@pytest.mark.parametrize("victim", ["registry_version", "current_code_sha"])
def test_candidate_never_raises_on_any_precondition_error(monkeypatch, victim) -> None:
    """The 'never raises' contract covers EVERY read, not just signing: a raise from a
    precondition helper (e.g. registry_version / current_code_sha) is caught → not eligible →
    a full review, never a crash of the plan review the gate runs under."""
    _setup(monkeypatch)  # all preconditions would otherwise pass

    def boom(*a, **k):
        raise RuntimeError("boom")

    if victim == "registry_version":
        monkeypatch.setattr(attest, "registry_version", boom)
    else:
        monkeypatch.setattr(llm_config, "current_code_sha", boom)
    d = attest.remediation_mode_candidate("T", window_minutes=_WINDOW, now_ns=_MIN_NS)
    assert d["eligible"] is False


# ── config keys (the retained remediation_window_minutes tuning param) ────────────────────────
def test_config_default_window_60_minutes() -> None:
    vc = core_config.VerifyConfig()
    assert vc.remediation_window_minutes == 60


def test_remediation_decision_always_proceeds(monkeypatch) -> None:
    """Remediation mode is always on (the off switch was retired in story 4cdf):
    ``_remediation_decision`` always proceeds to the eligibility candidate under an unmodified
    ``VerifyConfig()``. The former explicit-false back-out scenario no longer exists."""
    import types

    from rebar.llm import plan_review

    sentinel = {"eligible": True}
    monkeypatch.setattr(attest, "remediation_mode_candidate", lambda *a, **k: sentinel)

    monkeypatch.setattr(
        "rebar.config.load_config",
        lambda repo_root=None: types.SimpleNamespace(verify=core_config.VerifyConfig()),
    )
    assert plan_review._remediation_decision("T", None) is sentinel


def test_config_coerces_keys() -> None:
    sparse = core_config.coerce_sparse({"verify": {"remediation_window_minutes": 30}})
    assert sparse["verify"]["remediation_window_minutes"] == 30


def test_window_minutes_rejects_below_one() -> None:
    with pytest.raises(core_config.ConfigError):
        core_config.coerce_sparse({"verify": {"remediation_window_minutes": 0}})
