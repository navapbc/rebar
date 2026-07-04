"""Unit tests for the T2 semantic-resolution dispatch seam (epic 850f, story S2).

Pins the seam contract WITHOUT a concrete backend (pyright arrives in story S3):

* config parsing of the three ``.rebar/grounding.toml`` T2 keys (fail-open);
* ``refute_semantic`` default-off / no-backend behaviour (returns ``None``);
* dispatch to a monkeypatched fake backend → a ``refuted@T2`` record;
* the **default-off regression**: ``oracle.refute_absence`` on a member/dotted
  reference returns the byte-identical pre-epic ``abstain@T1`` record;
* escalation fires only for T2-territory references and only to upgrade an abstain;
* ``available_backends`` / ``grounding_info`` stay schema-valid.
"""

from __future__ import annotations

import pytest

import rebar
from rebar import schemas
from rebar.grounding import evidence as ev
from rebar.grounding import oracle, resolve, semantic

pytestmark = pytest.mark.unit


# ── config parsing (fail-open) ────────────────────────────────────────────────


def test_config_defaults_when_no_file(tmp_path) -> None:
    cfg = resolve.load_config(str(tmp_path))
    assert cfg.t2_enabled is False
    assert cfg.t2_backend is None
    assert cfg.t2_timeout_seconds == 30.0


def test_config_parses_valid_t2_keys(tmp_path) -> None:
    (tmp_path / ".rebar").mkdir()
    (tmp_path / ".rebar" / "grounding.toml").write_text(
        '[grounding]\nt2_enabled = true\nt2_backend = "pyright"\nt2_timeout_seconds = 12.5\n'
    )
    cfg = resolve.load_config(str(tmp_path))
    assert cfg.t2_enabled is True
    assert cfg.t2_backend == "pyright"
    assert cfg.t2_timeout_seconds == 12.5


def test_config_malformed_t2_keys_fail_open_to_defaults(tmp_path) -> None:
    (tmp_path / ".rebar").mkdir()
    (tmp_path / ".rebar" / "grounding.toml").write_text(
        "[grounding]\n"
        't2_enabled = "yes"\n'  # not a bool
        "t2_backend = 123\n"  # not a string
        "t2_timeout_seconds = -5\n"  # not positive
    )
    cfg = resolve.load_config(str(tmp_path))
    assert cfg.t2_enabled is False
    assert cfg.t2_backend is None
    assert cfg.t2_timeout_seconds == 30.0


def test_config_rejects_bool_timeout(tmp_path) -> None:
    # bool is an int subclass — it must not be accepted as a timeout number.
    (tmp_path / ".rebar").mkdir()
    (tmp_path / ".rebar" / "grounding.toml").write_text("[grounding]\nt2_timeout_seconds = true\n")
    assert resolve.load_config(str(tmp_path)).t2_timeout_seconds == 30.0


# ── refute_semantic: default-off / no backend ─────────────────────────────────

_MEMBER_REF = {"kind": "member", "name": "store.reconcile_tickets", "in_file": "a.py"}


def test_refute_semantic_disabled_returns_none(tmp_path) -> None:
    cfg = resolve.GroundingConfig(t2_enabled=False, t2_backend="pyright")
    assert semantic.refute_semantic(_MEMBER_REF, repo_root=str(tmp_path), config=cfg) is None


def test_refute_semantic_no_backend_selected_returns_none(tmp_path) -> None:
    cfg = resolve.GroundingConfig(t2_enabled=True, t2_backend=None)
    assert semantic.refute_semantic(_MEMBER_REF, repo_root=str(tmp_path), config=cfg) is None


def test_refute_semantic_unknown_backend_returns_none(tmp_path) -> None:
    cfg = resolve.GroundingConfig(t2_enabled=True, t2_backend="does-not-exist")
    assert semantic.refute_semantic(_MEMBER_REF, repo_root=str(tmp_path), config=cfg) is None


# ── dispatch to a fake backend ────────────────────────────────────────────────


def _fake_refuted(reference, *, repo_root, timeout, cache):
    return ev.refuted(
        provenance_tier=ev.TIER_T2,
        coverage=ev.coverage(backend="pyright", status=ev.STATUS_RAN, version="1.1.400"),
        reference=dict(reference),
        detail="fake semantic resolution",
    )


def test_dispatch_to_fake_backend_yields_refuted_t2(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(semantic, "_resolve_backend", lambda name: _fake_refuted)
    cfg = resolve.GroundingConfig(t2_enabled=True, t2_backend="pyright")
    rec = semantic.refute_semantic(_MEMBER_REF, repo_root=str(tmp_path), config=cfg)
    assert rec is not None
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["provenance_tier"] == ev.TIER_T2
    ev.validate(rec)  # a real T2 record validates against grounding.schema.json


def test_dispatch_backend_abstain_is_passed_through(monkeypatch, tmp_path) -> None:
    def fake_abstain(reference, *, repo_root, timeout, cache):
        return ev.abstain(
            "timeout", job=ev.JOB_REFUTE, provenance_tier=ev.TIER_T2, backend="pyright"
        )

    monkeypatch.setattr(semantic, "_resolve_backend", lambda name: fake_abstain)
    cfg = resolve.GroundingConfig(t2_enabled=True, t2_backend="pyright")
    rec = semantic.refute_semantic(_MEMBER_REF, repo_root=str(tmp_path), config=cfg)
    assert rec is not None
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["provenance_tier"] == ev.TIER_T2


def test_dispatch_backend_none_returns_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(semantic, "_resolve_backend", lambda name: lambda *a, **k: None)
    cfg = resolve.GroundingConfig(t2_enabled=True, t2_backend="pyright")
    assert semantic.refute_semantic(_MEMBER_REF, repo_root=str(tmp_path), config=cfg) is None


def test_backend_receives_effective_timeout(monkeypatch, tmp_path) -> None:
    seen = {}

    def fake(reference, *, repo_root, timeout, cache):
        seen["timeout"] = timeout
        return None

    monkeypatch.setattr(semantic, "_resolve_backend", lambda name: fake)
    cfg = resolve.GroundingConfig(t2_enabled=True, t2_backend="pyright", t2_timeout_seconds=7.0)
    semantic.refute_semantic(_MEMBER_REF, repo_root=str(tmp_path), config=cfg)
    assert seen["timeout"] == 7.0  # falls back to config when no explicit timeout


# ── the default-off regression through the oracle facade ──────────────────────


def test_oracle_member_ref_default_off_is_unchanged(tmp_path) -> None:
    """With T2 disabled (the default) a member/dotted ref returns abstain@T1."""
    rec = oracle.refute_absence(_MEMBER_REF, repo_root=str(tmp_path))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["provenance_tier"] == ev.TIER_T1  # NOT escalated to T2


def test_oracle_escalates_member_ref_when_enabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(semantic, "_resolve_backend", lambda name: _fake_refuted)
    cfg = resolve.GroundingConfig(t2_enabled=True, t2_backend="pyright")
    rec = oracle.refute_absence(_MEMBER_REF, repo_root=str(tmp_path), config=cfg)
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["provenance_tier"] == ev.TIER_T2


def test_oracle_keeps_t1_abstain_when_t2_abstains(monkeypatch, tmp_path) -> None:
    def fake_abstain(reference, *, repo_root, timeout, cache):
        return ev.abstain(
            "no_tool", job=ev.JOB_REFUTE, provenance_tier=ev.TIER_T2, backend="pyright"
        )

    monkeypatch.setattr(semantic, "_resolve_backend", lambda name: fake_abstain)
    cfg = resolve.GroundingConfig(t2_enabled=True, t2_backend="pyright")
    rec = oracle.refute_absence(_MEMBER_REF, repo_root=str(tmp_path), config=cfg)
    # T2 abstained → the T1 record is kept (T2 never downgrades / asserts absence).
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["provenance_tier"] == ev.TIER_T1


def test_oracle_does_not_escalate_non_t2_territory(monkeypatch, tmp_path) -> None:
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("refute_semantic must not run for non-T2 territory")

    monkeypatch.setattr(semantic, "refute_semantic", boom)
    cfg = resolve.GroundingConfig(t2_enabled=True, t2_backend="pyright")
    # a bare (non-dotted) file reference is not T2 territory
    rec = oracle.refute_absence(
        {"kind": "file", "name": "nope_missing.py"}, repo_root=str(tmp_path), config=cfg
    )
    assert called["n"] == 0
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN


def test_is_t2_territory() -> None:
    assert semantic.is_t2_territory({"kind": "member", "name": "a.b"})
    assert semantic.is_t2_territory({"kind": "symbol", "name": "Foo"})
    assert semantic.is_t2_territory({"kind": "import", "name": "pkg.mod"})
    assert semantic.is_t2_territory({"name": "obj.attr"})  # bare dotted name → member territory
    # file/dependency are never escalated (a file name's dot is an extension, not a member)
    assert not semantic.is_t2_territory({"kind": "file", "name": "obj.attr"})
    assert not semantic.is_t2_territory({"kind": "file", "name": "plain.py"})
    assert not semantic.is_t2_territory({"kind": "dependency", "name": "requests"})


# ── availability / grounding_info conformance ─────────────────────────────────


def test_available_backends_reports_pyright() -> None:
    got = semantic.available_backends()
    names = [b["name"] for b in got]
    assert names == ["pyright"]
    # availability tracks whether pyright is actually on PATH (may be absent here).
    assert isinstance(got[0]["available"], bool)


def test_available_backends_reports_registered(monkeypatch) -> None:
    monkeypatch.setattr(semantic, "T2_BACKENDS", ("pyright",))
    monkeypatch.setattr(semantic, "_backend_version", lambda name: "1.1.400")
    got = semantic.available_backends()
    assert got == [{"name": "pyright", "available": True, "version": "1.1.400"}]


def test_backend_availability_probe_is_fail_open(monkeypatch) -> None:
    def raiser(name):
        raise RuntimeError("boom")

    monkeypatch.setattr(semantic, "T2_BACKENDS", ("pyright",))
    monkeypatch.setattr(semantic, "_backend_version", raiser)
    # a raising probe must not crash the read tool — reports unavailable.
    assert semantic.available_backends() == [
        {"name": "pyright", "available": False, "version": None}
    ]


def test_grounding_info_still_validates_with_t2_backend(monkeypatch) -> None:
    monkeypatch.setattr(semantic, "T2_BACKENDS", ("pyright",))
    monkeypatch.setattr(semantic, "_backend_version", lambda name: "1.1.400")
    info = rebar.grounding_info()
    schemas.validator(schemas.GROUNDING_INFO).validate(info)
    names = [b["name"] for b in info["backends"]]
    assert "pyright" in names


def test_seam_dispatch_fake_backend_replaces_t1_abstain(monkeypatch, tmp_path) -> None:
    """A fake backend registered in ``T2_BACKENDS`` + injected via ``refute_semantic``
    produces a ``refuted@T2`` that REPLACES the member/dotted T1 abstain through the
    oracle — the end-to-end seam-dispatch demonstration."""
    monkeypatch.setattr(semantic, "T2_BACKENDS", ("fake",))

    def fake_refute_semantic(reference, *, repo_root, config, timeout=None, cache=None):
        return ev.normalize_evidence(
            _fake_refuted(reference, repo_root=repo_root, timeout=timeout, cache=cache)
        )

    monkeypatch.setattr(semantic, "refute_semantic", fake_refute_semantic)
    cfg = resolve.GroundingConfig(t2_enabled=True, t2_backend="fake")
    # T1 alone abstains on this member ref; the seam upgrades it to refuted@T2.
    rec = oracle.refute_absence(_MEMBER_REF, repo_root=str(tmp_path), config=cfg)
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["provenance_tier"] == ev.TIER_T2
    ev.validate(rec)  # the refuted@T2 record validates against grounding.schema.json
