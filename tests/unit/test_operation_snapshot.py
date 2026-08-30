"""Behavioral oracle for the RP-04 S1 ``OperationSnapshot`` composer.

RP-04 S1 (ticket a377) adds ``src/rebar/_operation_config.py``: one immutable,
serializable, non-secret configuration authority composed once per operation. The
snapshot carries validated non-secret effective values, explicit source-kind
provenance, the selected repository root, and an envelope version. It delegates
precedence/provenance to ``config.resolve_with_sources``, root selection to
``_config_sources.repo_root``, and canonical serialization/fingerprinting to
``_store.canonical`` — it must not reimplement any of them.

These are the AC2–AC4 unit/property oracles: precedence, root, immutability,
deterministic canonical serialization, fingerprint, envelope-version rejection, and
secret/live-object exclusion. Observable behavior only — no assertions on private
names or source text.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

import rebar.config as cfg
from rebar._operation_config import (
    ENVELOPE_VERSION,
    OperationSnapshot,
    compose_operation_snapshot,
)


# ── env isolation (mirrors tests/unit/test_config_loader.py) ──────────────────
@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("REBAR_CONFIG", "REBAR_ROOT", "REBAR_TRACKER_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    for key in list(__import__("os").environ):
        if key.startswith("REBAR_"):
            monkeypatch.delenv(key, raising=False)
    cfg.reset_config_cache()


def _proj(tmp: Path) -> Path:
    """A git-rooted project dir (repo_root falls back to git top-level of cwd)."""
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / ".git").mkdir()
    return tmp


# ── happy path: compose returns a populated, deterministic snapshot ───────────
def test_compose_returns_snapshot_with_values_sources_root_and_version(tmp_path: Path) -> None:
    snap = compose_operation_snapshot(repo_root=str(_proj(tmp_path)))
    assert isinstance(snap, OperationSnapshot)
    assert snap.envelope_version == ENVELOPE_VERSION
    assert Path(snap.repo_root) == tmp_path.resolve()
    # values mirror the resolved typed Config's effective non-secret values
    effective = dataclasses.asdict(cfg.load_config(str(tmp_path)))
    assert (
        snap.values["verify"]["max_ticket_description_chars"]
        == effective["verify"]["max_ticket_description_chars"]
    )
    # provenance is present for the same sections/keys
    assert set(snap.sources) == set(snap.values)
    assert snap.sources["verify"]["max_ticket_description_chars"] in {
        "default",
        "user",
        "project",
        "env",
        "cli",
    }


def test_fingerprint_is_deterministic_and_hex(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    a = compose_operation_snapshot(repo_root=str(p))
    b = compose_operation_snapshot(repo_root=str(p))
    fp = a.fingerprint()
    assert fp == b.fingerprint()
    assert isinstance(fp, str) and len(fp) == 64 and int(fp, 16) >= 0


# ── AC3: precedence — explicit input wins each pairwise case ──────────────────
@pytest.mark.parametrize(
    ("higher_writer", "expect_source"),
    [
        ("cli", "cli"),
        ("env", "env"),
        ("project", "project"),
    ],
)
def test_precedence_higher_layer_wins_over_project_and_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, higher_writer: str, expect_source: str
) -> None:
    p = _proj(tmp_path)
    # project layer sets sync.push=always
    (p / "rebar.toml").write_text("[sync]\npush = 'always'\n", encoding="utf-8")
    cli_overrides = None
    if higher_writer == "cli":
        cli_overrides = {"sync": {"push": "off"}}
    elif higher_writer == "env":
        monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    snap = compose_operation_snapshot(repo_root=str(p), cli_overrides=cli_overrides)
    if higher_writer == "project":
        assert snap.values["sync"]["push"] == "always"
    else:
        assert snap.values["sync"]["push"] == "off"
    assert snap.sources["sync"]["push"] == expect_source


def test_precedence_cli_outranks_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _proj(tmp_path)
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    snap = compose_operation_snapshot(repo_root=str(p), cli_overrides={"sync": {"push": "off"}})
    assert snap.values["sync"]["push"] == "off"
    assert snap.sources["sync"]["push"] == "cli"


# ── AC3: root selection is independent of ambient cwd ─────────────────────────
def test_explicit_root_A_used_not_ambient_root_B(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_a = _proj(tmp_path / "A")
    root_b = _proj(tmp_path / "B")
    (root_a / "rebar.toml").write_text("[sync]\npush = 'off'\n", encoding="utf-8")
    (root_b / "rebar.toml").write_text("[sync]\npush = 'always'\n", encoding="utf-8")
    monkeypatch.chdir(root_b)
    snap = compose_operation_snapshot(repo_root=str(root_a))
    assert Path(snap.repo_root) == root_a.resolve()
    assert snap.values["sync"]["push"] == "off"  # A's project config, not B's


def test_rebar_root_env_selects_root_when_no_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_a = _proj(tmp_path / "A")
    monkeypatch.setenv("REBAR_ROOT", str(root_a))
    snap = compose_operation_snapshot()
    assert Path(snap.repo_root) == root_a.resolve()


# ── AC4: immutability ─────────────────────────────────────────────────────────
def test_snapshot_is_frozen(tmp_path: Path) -> None:
    snap = compose_operation_snapshot(repo_root=str(_proj(tmp_path)))
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.envelope_version = 999  # type: ignore[misc]


def test_snapshot_values_mapping_is_read_only(tmp_path: Path) -> None:
    snap = compose_operation_snapshot(repo_root=str(_proj(tmp_path)))
    with pytest.raises((TypeError, AttributeError)):
        snap.values["sync"]["push"] = "hacked"  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        snap.sources["sync"]["push"] = "hacked"  # type: ignore[index]


def test_projection_exposes_only_named_sections_and_is_read_only(tmp_path: Path) -> None:
    snap = compose_operation_snapshot(repo_root=str(_proj(tmp_path)))
    proj = snap.project("sync")
    assert set(proj.values) == {"sync"}
    assert proj.values["sync"] == snap.values["sync"]
    with pytest.raises((TypeError, AttributeError)):
        proj.values["sync"]["push"] = "hacked"  # type: ignore[index]
    with pytest.raises(KeyError):
        snap.project("no_such_section")


# ── AC4: deterministic canonical serialization + envelope round-trip ──────────
def test_canonical_document_is_sorted_and_versioned(tmp_path: Path) -> None:
    snap = compose_operation_snapshot(repo_root=str(_proj(tmp_path)))
    doc = snap.canonical_document()
    assert doc["envelope_version"] == ENVELOPE_VERSION
    # canonical bytes are compact + sorted (delegated to _store.canonical)
    from rebar._store import canonical as canon

    assert snap.canonical_bytes() == canon.canonical_bytes(doc)
    assert snap.fingerprint() == canon.content_hash(doc)


def test_snapshot_round_trips_through_canonical_document(tmp_path: Path) -> None:
    snap = compose_operation_snapshot(repo_root=str(_proj(tmp_path)))
    restored = OperationSnapshot.from_document(snap.canonical_document())
    assert restored.fingerprint() == snap.fingerprint()
    assert restored.values == snap.values
    assert restored.sources == snap.sources


def test_unknown_envelope_version_is_rejected(tmp_path: Path) -> None:
    snap = compose_operation_snapshot(repo_root=str(_proj(tmp_path)))
    doc = dict(snap.canonical_document())
    doc["envelope_version"] = ENVELOPE_VERSION + 1
    with pytest.raises(cfg.ConfigError):
        OperationSnapshot.from_document(doc)


# ── AC4: secret / live-object exclusion ───────────────────────────────────────
def test_build_rejects_secret_wrapper_values(tmp_path: Path) -> None:
    pydantic = pytest.importorskip("pydantic")
    poisoned = {"jira": {"api_token": pydantic.SecretStr("s3cr3t-sentinel")}}
    with pytest.raises((TypeError, ValueError)):
        OperationSnapshot.build(
            envelope_version=ENVELOPE_VERSION,
            repo_root=str(_proj(tmp_path)),
            values=poisoned,
            sources={"jira": {"api_token": "env"}},
        )


def test_build_rejects_live_capability_object(tmp_path: Path) -> None:
    class _LiveClient:  # a live provider/client double
        def __repr__(self) -> str:  # would smuggle material into repr/serialization
            return "sk-live-sentinel"

    with pytest.raises((TypeError, ValueError)):
        OperationSnapshot.build(
            envelope_version=ENVELOPE_VERSION,
            repo_root=str(_proj(tmp_path)),
            values={"llm": {"client": _LiveClient()}},
            sources={"llm": {"client": "cli"}},
        )


def test_secret_sentinels_absent_from_every_named_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = "SECRET-SENTINEL-DO-NOT-LEAK"
    for name in (
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "JIRA_API_TOKEN",
        "GERRIT_BOT_TOKEN",
        "GITHUB_TOKEN",
        "LANGFUSE_SECRET_KEY",
    ):
        monkeypatch.setenv(name, sentinel)
    snap = compose_operation_snapshot(repo_root=str(_proj(tmp_path)))
    boundaries = [
        repr(snap),
        str(snap.values),
        str(snap.sources),
        snap.canonical_bytes().decode("utf-8"),
        snap.fingerprint(),
    ]
    for boundary in boundaries:
        assert sentinel not in boundary
