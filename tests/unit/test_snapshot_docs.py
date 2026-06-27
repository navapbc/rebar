"""S6 — docs for the repo-snapshot code-reading gates (epic raze-vet-ditch).

The ACs are deliberately grep-able against NAMED files; these assertions pin exactly that
so the documentation can't silently regress (credential requirement, descriptive errors,
ref/source semantics, HMAC trust model + in-toto shape, the env knobs + EFS/NFS caveat).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text().lower()


# AC1 — README (MCP setup) + jira-sync-setup state the private-repo fetch-credential need
def test_credential_requirement_documented_in_named_files():
    for rel in ("README.md", "docs/jira-sync-setup.md"):
        text = _read(rel)
        assert "credential" in text
        assert "fetch" in text and "private" in text
        assert "repo-snapshot-gates.md" in text  # cross-ref to the full doc


# AC2 — descriptive credential-failure behavior + fail-closed (attested) vs local-still-runs
def test_error_behavior_documented():
    text = _read("docs/repo-snapshot-gates.md")
    assert "fail" in text and "closed" in text
    assert "snapshotfetcherror" in text
    assert "local" in text and "never fetches" in text


# AC3 — ref/source semantics, origin/main default, signs vs never-signs, verified_at_sha
def test_ref_source_semantics_documented():
    text = _read("docs/repo-snapshot-gates.md")
    assert "attested" in text and "local" in text
    assert "origin/main" in text
    assert "verified_at_sha" in text
    assert "never" in text and "signed" in text  # local NEVER signed; attested signs


# AC4 — HMAC trust model + limits + the in-toto Statement shape
def test_hmac_trust_model_documented():
    text = _read("docs/repo-snapshot-gates.md")
    assert "hmac-sha256" in text
    assert "non-repudiation" in text  # the documented limit
    assert "in-toto" in text and "dsse" in text


# The canonical config doc (docs/config.md) documents the [snapshot] section + flags
def test_config_md_documents_snapshot_section_and_flags():
    text = _read("docs/config.md")
    assert "[snapshot]" in text
    for env in (
        "rebar_gate_ref",
        "rebar_gate_source",
        "rebar_gate_tmpdir",
        "rebar_gate_free_watermark_bytes",
        "rebar_gate_grace_seconds",
        "rebar_gate_max_age_seconds",
        "rebar_gate_reverify_seconds",
        "rebar_gate_janitor_interval_seconds",
    ):
        assert env in text, f"docs/config.md missing {env}"
    assert "--ref" in text and "--source" in text


# AC5 — REBAR_GATE_TMPDIR, disk-cap behavior, EFS/NFS flock caveat (xref alto-fruit-punch)
def test_env_knobs_and_flock_caveat_documented():
    text = _read("docs/repo-snapshot-gates.md")
    assert "rebar_gate_tmpdir" in text
    assert "watermark" in text and "reclaim" in text  # disk-cap behavior
    assert "efs" in text and "nfs" in text and "flock" in text
    assert "alto-fruit-punch" in text  # the cross-reference
