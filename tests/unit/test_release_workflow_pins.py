"""Rebar-specific release.yml invariants that zizmor/actionlint cannot know (story 08a8).

zizmor enforces the GENERIC action-security checks (full-SHA pins, persist-credentials:false,
over-broad/OIDC permissions) under `make lint`. This test pins only the rebar-specific
structure: the `--no-isolation` build, the mcp_verify/mcp_registry OIDC split, the chmod,
the evidence-fragment pipeline, the ambient-package guard step, the single annotated
zizmor suppression, the verify-mcp-pin wiring, and the docs "Pinning" substrings.

Static assertions over the workflow text + parsed YAML — no release run needed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import rebar

ROOT = Path(rebar.__file__).resolve().parents[2]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
RELEASING_DOC = ROOT / "docs" / "releasing.md"


def _text() -> str:
    return RELEASE.read_text(encoding="utf-8")


def _wf() -> dict:
    return yaml.safe_load(_text())


def _job(name: str) -> dict:
    jobs = _wf()["jobs"]
    assert name in jobs, f"release.yml is missing the `{name}` job"
    return jobs[name]


# ── (a/b) every `uses:` is full-SHA pinned (belt-and-suspenders to zizmor) ─────
def test_all_uses_are_full_sha_pinned() -> None:
    import re

    uses = re.findall(r"uses:\s*(\S+)", _text())
    assert uses, "expected `uses:` steps in release.yml"
    for u in uses:
        ref = u.split("@", 1)[1] if "@" in u else ""
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (
            f"`uses: {u}` is not pinned to a full 40-char commit SHA"
        )


def test_checkouts_disable_persist_credentials() -> None:
    text = _text()
    # Every checkout step must set persist-credentials: false.
    import re

    n_checkouts = len(re.findall(r"uses:\s*actions/checkout@", text))
    n_disabled = len(re.findall(r"persist-credentials:\s*false", text))
    assert n_disabled >= n_checkouts, (
        f"{n_checkouts} checkout(s) but only {n_disabled} persist-credentials:false"
    )


# ── (c) hash-locked --no-isolation build ──────────────────────────────────────
def test_build_uses_no_isolation() -> None:
    assert "--no-isolation" in _text(), "build must use `python -m build --no-isolation`"
    assert "--require-hashes" in _text(), "install must use --require-hashes against the lock"
    assert ".github/release-requirements.txt" in _text()


# ── (h) ambient-package guard step wired ──────────────────────────────────────
def test_check_build_env_locked_step_wired() -> None:
    assert "check_build_env_locked.py" in _text(), "the ambient-package guard step is missing"


# ── (e) mcp_verify/mcp_registry OIDC split ────────────────────────────────────
def test_mcp_verify_registry_split() -> None:
    verify = _job("mcp_verify")
    registry = _job("mcp_registry")
    # mcp_verify holds NO id-token: write; mcp_registry does.
    v_perms = verify.get("permissions", {}) or {}
    r_perms = registry.get("permissions", {}) or {}
    assert v_perms.get("id-token") != "write", "mcp_verify must NOT have id-token: write"
    assert r_perms.get("id-token") == "write", "mcp_registry must have id-token: write"
    # mcp_registry consumes the verified artifact from mcp_verify.
    needs = registry.get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    assert "mcp_verify" in needs, "mcp_registry must `needs: mcp_verify`"


# ── (g) chmod +x after download (upload-artifact drops the exec bit) ───────────
def test_chmod_exec_bit_restored() -> None:
    registry = _job("mcp_registry")
    steps_text = yaml.safe_dump(registry)
    assert "chmod +x mcp-publisher" in steps_text, (
        "mcp_registry must `chmod +x mcp-publisher` after download-artifact"
    )


# ── download -> verify -> execute, never `curl | tar` ─────────────────────────
def test_no_curl_pipe_tar() -> None:
    text = _text()
    import re

    # The old one-liner `curl … | tar xz` must be gone (unverified extract/execute).
    assert not re.search(r"curl[^\n]*\|\s*tar", text), (
        "an unverified `curl … | tar` archive stream must not be present"
    )
    assert "sha256sum -c" in text, "the archive must be digest-verified before extraction"


# ── one annotated zizmor suppression, and no other ────────────────────────────
def test_single_annotated_zizmor_suppression() -> None:
    text = _text()
    import re

    ignores = re.findall(r"# *zizmor:ignore\[([^\]]+)\]", text)
    assert ignores == ["excessive-permissions"], (
        f"exactly one zizmor:ignore[excessive-permissions] is allowed, found: {ignores}"
    )
    # It must be justified (mentions mcp-publisher / oidc).
    assert re.search(
        r"zizmor:ignore\[excessive-permissions\][^\n]*(mcp-publisher|oidc)", text, re.I
    )


# ── (f) evidence-fragment pipeline + consolidation ────────────────────────────
def test_evidence_fragments_and_consolidation() -> None:
    text = _text()
    for frag in ("evidence-shapin", "evidence-build", "evidence-mcp"):
        assert frag in text, f"evidence fragment `{frag}` is not wired in release.yml"
    # A consolidation job downloads the fragments and concatenates them.
    assert "release-evidence.txt" in text, "the consolidated release-evidence.txt is missing"


# ── verify-mcp-pin wiring (the [operator-attested] correctness check) ──────────
def test_verify_mcp_pin_script_and_target() -> None:
    assert (ROOT / "scripts" / "verify_mcp_publisher_pin.py").exists(), (
        "scripts/verify_mcp_publisher_pin.py is missing"
    )
    assert "verify-mcp-pin" in (ROOT / "Makefile").read_text(), (
        "the `make verify-mcp-pin` target is missing"
    )


# ── (d) docs/releasing.md "Pinning" section substrings ────────────────────────
def test_docs_pinning_section() -> None:
    doc = RELEASING_DOC.read_text(encoding="utf-8")
    for needle in ("pip-compile --generate-hashes", "MCP publisher", "full-SHA"):
        assert needle in doc, f"docs/releasing.md Pinning section missing `{needle}`"


# ══════════════════════════════════════════════════════════════════════════════
#  Story 6168 (build-once) extensions — checks (g)/(h)/(i)
# ══════════════════════════════════════════════════════════════════════════════
def test_g_publish_hash_gated_no_rebuild() -> None:
    """(g) The publish job verifies the bundle with `sha256sum -c SHA256SUMS` and does NOT
    rebuild (no `python -m build` in publish) — it promotes the exact built bytes."""
    registry = _wf()["jobs"]
    assert "publish" in registry, "release.yml missing the publish job"
    pub = yaml.safe_dump(registry["publish"])
    assert "sha256sum -c SHA256SUMS" in pub, "publish must hash-gate the bundle before publishing"
    assert "python -m build" not in pub, "publish must NOT rebuild — promote the built bytes"


def test_h_wheel_and_sdist_test_jobs_with_probes() -> None:
    """(h) A wheel-test job and an sdist-test job exist, each needs: the build-once job and
    runs the named probes; sdist-test runs with REBAR_BUILD_COMMIT unset and asserts a
    non-null baked COMMIT."""
    jobs = _wf()["jobs"]
    names = set(jobs)
    wheel_job = next((n for n in names if "wheel" in n and "test" in n), None)
    sdist_job = next((n for n in names if "sdist" in n and "test" in n), None)
    assert wheel_job, "a wheel-test job is missing"
    assert sdist_job, "an sdist-test job is missing"
    wt = yaml.safe_dump(jobs[wheel_job])
    st = yaml.safe_dump(jobs[sdist_job])
    for probe in ("import rebar", "rebar --help", "rebar-mcp --help", "_build_info"):
        assert probe in wt, f"wheel-test job missing probe: {probe}"
    assert "_build_info.COMMIT" in wt, "wheel-test must assert a non-null baked COMMIT"
    assert "_build_info.COMMIT" in st, "sdist-test must assert a non-null baked COMMIT"


def test_i_build_once_bundle_with_sha256sums() -> None:
    """(i) A single build-once job produces one bundle containing the wheel, the sdist, and a
    SHA256SUMS file, and uploads the evidence-artifacts fragment consumed by consolidate."""
    text = _text()
    assert "SHA256SUMS" in text, "the build-once job must produce a SHA256SUMS file"
    assert "REBAR_BUILD_COMMIT" in text, (
        "the build-once job must set REBAR_BUILD_COMMIT before build"
    )
    assert "evidence-artifacts" in text, (
        "the evidence-artifacts fragment (wheel/sdist sums) is missing"
    )
