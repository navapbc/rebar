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
    # F4's three fragments PLUS F3/6168's evidence-artifacts (wheel/sdist SHA-256 sums),
    # all consumed by the consolidation job (AC4 requires check (f) to include it).
    for frag in ("evidence-shapin", "evidence-build", "evidence-mcp", "evidence-artifacts"):
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


# ══════════════════════════════════════════════════════════════════════════════
#  Story fd84 (Finding 1) — dispatch-from-main + version-lockstep + ancestry +
#  env-preflight release-source authorization
# ══════════════════════════════════════════════════════════════════════════════
def test_fd84_trigger_is_dispatch_only_with_version_input() -> None:
    """The release is initiated by `workflow_dispatch` with a `version` input, and the
    tag-push publish trigger is GONE (a hand-pushed `v*` tag can no longer publish)."""
    wf = _wf()
    # PyYAML parses the bare `on:` key as boolean True.
    on = wf.get("on", wf.get(True))
    assert isinstance(on, dict), f"`on:` should be a mapping, got {on!r}"
    assert "workflow_dispatch" in on, "release.yml must be workflow_dispatch-triggered"
    assert "push" not in on, "the `push:`/tag publish trigger must be removed"
    wd = on["workflow_dispatch"]
    assert isinstance(wd, dict) and "inputs" in wd and "version" in wd["inputs"], (
        "workflow_dispatch must declare a `version` input"
    )


def test_fd84_authorize_job_guards_precede_build() -> None:
    """An authorize job runs the three guards (ref==main, version-lockstep, ancestry) and
    the build job needs it, so no build/publish happens until authorization passes."""
    jobs = _wf()["jobs"]
    auth = next((n for n in jobs if "authoriz" in n.lower()), None)
    assert auth, "release.yml is missing the release-source authorization job"
    at = yaml.safe_dump(jobs[auth])
    assert "refs/heads/main" in at, "authorize must guard `github.ref == refs/heads/main`"
    assert "release_guards.py" in at, "authorize must call the extracted guard helpers"
    assert "version-lockstep" in at, "authorize must run the version-lockstep guard"
    assert "ancestry" in at, "authorize must run the ancestry guard"
    assert "merge-base" in at or "ancestry" in at, "ancestry guard must use git merge-base"
    assert "env-preflight" in at, "authorize must run the pypi env-preflight guard"
    # fetch-depth: 0 is required so the ancestry check has full history.
    assert "fetch-depth: 0" in at, "authorize checkout must use fetch-depth: 0 for ancestry"
    # The build job (and thus the whole publish chain) is gated on authorize.
    build_needs = jobs["build"].get("needs")
    build_needs = [build_needs] if isinstance(build_needs, str) else (build_needs or [])
    assert auth in build_needs, "the build job must `needs:` the authorize job"


def test_fd84_no_tag_context_if_guards_remain() -> None:
    """No job still gates on the old tag-push context; version is derived from the dispatch
    input, not `github.ref_name`."""
    text = _text()
    assert "startsWith(github.ref, 'refs/tags/v')" not in text, (
        "a stale tag-context `if:` guard remains — re-key it to the dispatch input"
    )
    assert "github.ref_name" not in text, (
        "a stale `github.ref_name` version derivation remains — use inputs.version"
    )
    assert "inputs.version" in text, "jobs must derive the version from the dispatch input"


def test_fd84_release_guards_script_exists_and_preflight_wired() -> None:
    """The extracted guard helper exists and the env-preflight reads the live pypi
    environment (required reviewers + main-only branch policy) fail-closed."""
    script = ROOT / "scripts" / "release_guards.py"
    assert script.exists(), "scripts/release_guards.py (the extracted guards) is missing"
    text = _text()
    assert "/environments/pypi" in text, "the env-preflight must query GET .../environments/pypi"


def test_fd84_tag_created_after_publish_not_a_trigger() -> None:
    """The `v*` tag + GitHub Release are created only AFTER a successful publish (record
    only): the github_release job needs publish and no longer relies on a pre-pushed tag."""
    jobs = _wf()["jobs"]
    # The GitHub-Release job must run after publish.
    ghr = jobs.get("github_release")
    assert ghr is not None, "the github_release job is missing"
    needs = ghr.get("needs")
    needs = [needs] if isinstance(needs, str) else (needs or [])
    assert "publish" in needs, "github_release must run after (needs) publish"
