"""Signing-call-site tests for the attestations epic (story 2c2d, epic dark-acme-lumen).

Pins that the two signers stamp the unsigned ``data["kind"]`` routing hint, that the hint
does not enter the signed payload (so old signatures still verify and there's no
PAYLOAD_VERSION bump), and that the completion manifest records the material fingerprint so
completion validity-on-read can detect post-signing material edits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar._commands.transition_close import _verdict_manifest


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-2c2d")
    rebar.init_repo(repo_root=str(repo))
    return repo


def test_sign_manifest_kind_routes_to_attestations_and_verifies(store: Path) -> None:
    tid = rebar.create_ticket("task", "kind passthrough", repo_root=str(store))
    rec = signing.sign_manifest(
        tid, ["plan-review: PASS", "material: m"], kind="plan-review", repo_root=str(store)
    )
    assert rec["kind"] == "plan-review"
    # Still HMAC-certified — the unsigned hint is not part of the signed payload.
    assert signing.verify_signature(tid, repo_root=str(store))["verdict"] == "certified"
    # Reduced into the kind-keyed map under the (manifest-authoritative) kind.
    state = rebar.show_ticket(tid, repo_root=str(store))
    assert list(state["attestations"]) == ["plan-review"]


def test_sign_manifest_without_kind_omits_field(store: Path) -> None:
    # Back-compat: a caller that does not pass kind signs exactly as before (no kind key).
    tid = rebar.create_ticket("task", "no kind", repo_root=str(store))
    rec = signing.sign_manifest(tid, ["plan-review: PASS"], repo_root=str(store))
    assert "kind" not in rec
    # The reducer still derives the kind from the signed manifest[0].
    state = rebar.show_ticket(tid, repo_root=str(store))
    assert list(state["attestations"]) == ["plan-review"]


def test_verdict_manifest_records_material_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    # The completion manifest must carry the material fingerprint (symmetric with plan-review)
    # so completion validity-on-read can detect a post-signing material edit.
    monkeypatch.setattr(
        "rebar.llm.plan_review.attest.current_material_fingerprint",
        lambda ticket_id, repo_root=None: "fp-abc123",
    )
    manifest = _verdict_manifest({"model": "m", "runner": "r"}, "tid-1", repo_root="/x")
    assert manifest[0] == "completion-verifier: PASS"
    assert "material: fp-abc123" in manifest


def test_verdict_manifest_omits_material_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "rebar.llm.plan_review.attest.current_material_fingerprint",
        lambda ticket_id, repo_root=None: None,
    )
    manifest = _verdict_manifest({"model": "m", "runner": "r"}, "tid-1", repo_root="/x")
    assert manifest[0] == "completion-verifier: PASS"
    assert not any(line.startswith("material:") for line in manifest)


# ── gate-code version+SHA provenance stamp (epic jira-reb-596) ─────────────────────────
def _load_hatch_build():
    """Load hatch_build.py by path — it lives at the repo root, not in the installed
    package, so `import hatch_build` is not reliable under CI's sys.path."""
    import importlib.util

    pytest.importorskip("hatchling")  # hatch_build imports BuildHookInterface at module load
    repo_root = Path(rebar.__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("hatch_build", repo_root / "hatch_build.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git_repo(path: Path) -> Path:
    """A tmp git checkout with one commit — a stand-in for a live rebar source checkout."""
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=path, check=True)
    (path / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=path, check=True)
    return path


def test_gate_code_version_live_checkout_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _git_repo(tmp_path / "src")
    monkeypatch.setattr("importlib.metadata.version", lambda *a, **k: "1.2.3")
    got = signing.gate_code_version(source_dir=str(src))
    # "<version> (<short-sha>)" — no -dirty on a clean tree.
    assert got.startswith("1.2.3 (")
    assert got.endswith(")") and "-dirty" not in got


def test_gate_code_version_live_checkout_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _git_repo(tmp_path / "src")
    (src / "untracked.py").write_text("y = 2\n")  # dirty the working tree
    monkeypatch.setattr("importlib.metadata.version", lambda *a, **k: "1.2.3")
    assert signing.gate_code_version(source_dir=str(src)).endswith("-dirty)")


def test_gate_code_version_non_git_omits_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "nogit"
    plain.mkdir()
    monkeypatch.setattr("importlib.metadata.version", lambda *a, **k: "1.2.3")
    monkeypatch.setattr(signing, "_baked_commit_sha", lambda: None)  # no wheel-baked SHA either
    # No live checkout and no baked SHA -> version only.
    assert signing.gate_code_version(source_dir=str(plain)) == "1.2.3"


def test_gate_code_version_falls_back_to_baked_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Story 2: a non-git (wheel/PyPI) install with a build-baked SHA records that SHA."""
    plain = tmp_path / "nogit"
    plain.mkdir()
    monkeypatch.setattr("importlib.metadata.version", lambda *a, **k: "1.2.3")
    monkeypatch.setattr(signing, "_baked_commit_sha", lambda: "bakedsha")
    assert signing.gate_code_version(source_dir=str(plain)) == "1.2.3 (bakedsha)"


def test_gate_code_version_prefers_live_over_baked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live checkout wins over the baked SHA (the baked value can be stale in an editable
    install that has since advanced)."""
    src = _git_repo(tmp_path / "src")
    monkeypatch.setattr("importlib.metadata.version", lambda *a, **k: "1.2.3")
    monkeypatch.setattr(signing, "_baked_commit_sha", lambda: "STALEBAKED")
    got = signing.gate_code_version(source_dir=str(src))
    assert "STALEBAKED" not in got and got.startswith("1.2.3 (")


def test_baked_commit_sha_reads_build_info(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    mod = types.ModuleType("rebar._build_info")
    mod.COMMIT = "deadbeef"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rebar._build_info", mod)
    assert signing._baked_commit_sha() == "deadbeef"
    mod.COMMIT = None  # type: ignore[attr-defined]
    assert signing._baked_commit_sha() is None


def test_build_hook_writes_build_info(tmp_path: Path) -> None:
    """The hatchling build hook bakes the source-tree HEAD SHA into src/rebar/_build_info.py."""
    hatch_build = _load_hatch_build()

    root = _git_repo(tmp_path / "proj")
    (root / "src" / "rebar").mkdir(parents=True)
    commit = hatch_build._build_commit(root)
    assert commit and len(commit) >= 4
    target = root / "src" / "rebar" / "_build_info.py"
    target.write_text(f"COMMIT = {commit!r}\n")
    ns: dict = {}
    exec(target.read_text(), ns)  # noqa: S102 — reading back the generated module
    assert ns["COMMIT"] == commit


def test_build_hook_non_git_commit_none(tmp_path: Path) -> None:
    hatch_build = _load_hatch_build()

    plain = tmp_path / "nogit"
    plain.mkdir()
    assert hatch_build._build_commit(plain) is None


def test_built_wheel_bakes_build_info(tmp_path: Path) -> None:
    """AC (story 2): the FULL hatchling build cycle produces a wheel that actually contains
    src/rebar/_build_info.py with a baked commit SHA — not just the unit-level hook helper.
    Mirrors tests/unit/test_engine_dir.py::test_wheel_contains_no_compiled_bytecode."""
    import re
    import zipfile

    hatchling_wheel = pytest.importorskip("hatchling.builders.wheel")

    repo_root = Path(rebar.__file__).resolve().parents[2]
    assert (repo_root / "pyproject.toml").is_file(), repo_root
    generated = repo_root / "src" / "rebar" / "_build_info.py"
    pre_existing = generated.exists()
    try:
        builder = hatchling_wheel.WheelBuilder(str(repo_root))
        wheels = [p for p in builder.build(directory=str(tmp_path)) if str(p).endswith(".whl")]
        assert wheels, "no wheel produced"
        with zipfile.ZipFile(wheels[0]) as zf:
            members = [n for n in zf.namelist() if n.endswith("rebar/_build_info.py")]
            assert members, "the built wheel must contain the baked src/rebar/_build_info.py"
            content = zf.read(members[0]).decode()
    finally:
        if not pre_existing and generated.exists():
            generated.unlink()  # clean the git-ignored build artifact
    # Built from this git checkout -> COMMIT is a real short SHA, not None.
    assert re.search(r"COMMIT = '[0-9a-f]{4,}'", content), content


# ── story 3: surface the stamp in verify_signature + show ─────────────────────────────
def test_verify_signature_returns_rebar_version(store: Path) -> None:
    tid = rebar.create_ticket("task", "stamp surfacing", repo_root=str(store))
    signing.sign_manifest(
        tid,
        ["plan-review: PASS", signing.rebar_version_step("7.7.7 (cafef00d)")],
        kind="plan-review",
        repo_root=str(store),
    )
    result = rebar.verify_signature(tid, repo_root=str(store))
    assert result["verdict"] == "certified"
    assert result["rebar_version"] == "7.7.7 (cafef00d)"


def test_verify_signature_rebar_version_none_for_pre_stamp(store: Path) -> None:
    tid = rebar.create_ticket("task", "pre-stamp", repo_root=str(store))
    # A manifest with no rebar-version step (a pre-jira-reb-596 signature).
    signing.sign_manifest(tid, ["plan-review: PASS"], kind="plan-review", repo_root=str(store))
    result = rebar.verify_signature(tid, repo_root=str(store))
    assert result["verdict"] == "certified"
    assert result["rebar_version"] is None


def test_verify_signature_rebar_version_none_when_unsigned(store: Path) -> None:
    tid = rebar.create_ticket("task", "unsigned", repo_root=str(store))
    result = rebar.verify_signature(tid, repo_root=str(store))
    assert result["verdict"] == "unsigned"
    assert result["rebar_version"] is None


def test_show_displays_rebar_version_stamp(store: Path) -> None:
    tid = rebar.create_ticket("task", "show stamp", repo_root=str(store))
    signing.sign_manifest(
        tid,
        ["plan-review: PASS", signing.rebar_version_step("8.8.8 (b4dc0de-dirty)")],
        kind="plan-review",
        repo_root=str(store),
    )
    state = rebar.show_ticket(tid, repo_root=str(store))
    manifest = state["attestations"]["plan-review"]["manifest"]
    # The stamp is displayed in the show attestation block via the signed manifest line.
    assert any(line.startswith("rebar-version: 8.8.8 (b4dc0de-dirty)") for line in manifest)
    assert signing.rebar_version_from_manifest(manifest) == "8.8.8 (b4dc0de-dirty)"


def test_verify_signature_result_model_documents_rebar_version() -> None:
    """The typed MCP contract (VerifySignatureResultOut) carries the field (epic jira-reb-596)."""
    models = pytest.importorskip("rebar._mcp_models")
    model = models.VerifySignatureResultOut
    if model is None:  # pydantic unavailable in this env
        pytest.skip("pydantic not installed")
    field = model.model_fields.get("rebar_version")
    assert field is not None, "VerifySignatureResultOut must document rebar_version"
    assert not field.is_required(), "rebar_version must be optional for pre-stamp back-compat"


def test_both_manifests_carry_rebar_version_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signing, "gate_code_version", lambda source_dir=None: "9.9.9 (deadbee)")
    monkeypatch.setattr(
        "rebar.llm.plan_review.attest.current_material_fingerprint",
        lambda ticket_id, repo_root=None: None,
    )
    from rebar.llm.plan_review import attest

    plan = attest.build_manifest({"verdict": "PASS", "ticket_id": "t"}, material="m")
    completion = _verdict_manifest({"model": "m", "runner": "r"}, "tid-1", repo_root="/x")
    assert "rebar-version: 9.9.9 (deadbee)" in plan
    assert "rebar-version: 9.9.9 (deadbee)" in completion


def test_rebar_version_parser_roundtrip_and_pre_stamp_none() -> None:
    from rebar.llm.plan_review import attest

    stamped = [signing.rebar_version_step("0.6.0 (abc123-dirty)"), "ticket: t"]
    assert signing.rebar_version_from_manifest(stamped) == "0.6.0 (abc123-dirty)"
    assert attest.manifest_rebar_version(stamped) == "0.6.0 (abc123-dirty)"
    # A manifest signed before the stamp existed parses to None (no crash).
    assert signing.rebar_version_from_manifest(["plan-review: PASS", "ticket: t"]) is None
    assert attest.manifest_rebar_version(["plan-review: PASS"]) is None
    assert signing.rebar_version_from_manifest(None) is None


def test_rebar_version_stamp_is_inert_to_validity_parsers() -> None:
    """The stamp is provenance-only: adding it must not change the values compute_validity
    reads (material / regver / deps). Proven by parsing a manifest with and without the line."""
    from rebar.llm.plan_review import attest

    base = attest.build_manifest(
        {"verdict": "PASS", "ticket_id": "t"},
        material="fp-1",
        regver="rv-1",
        deps={"a.py": "h1"},
    )
    without = [ln for ln in base if not ln.startswith(signing.REBAR_VERSION_PREFIX)]
    assert attest.manifest_material(base) == attest.manifest_material(without) == "fp-1"
    assert attest.manifest_regver(base) == attest.manifest_regver(without) == "rv-1"
    assert attest.manifest_deps(base) == attest.manifest_deps(without) == {"a.py": "h1"}


# ── build-provenance hook precedence (story 6168) ─────────────────────────────
# Fast, build-free unit coverage of hatch_build._resolve_build_commit — the four-step
# precedence (env → preserve-existing → git → None) and the release-context fail-fast
# on a set-but-empty REBAR_BUILD_COMMIT. The slow build-based oracle (a real
# `python -m build` sdist→wheel round-trip) lives in tests/unit/test_build_provenance.py
# and is additionally exercised by release.yml's wheel-test / sdist-test jobs.
def _hook_module():
    import importlib.util

    root = Path(rebar.__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("_hb_probe", root / "hatch_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_build_commit_env_var_wins() -> None:
    mod = _hook_module()
    resolved = mod._resolve_build_commit(
        Path("/does/not/matter"), existing="oldsha0", env={"REBAR_BUILD_COMMIT": "abc1234"}
    )
    assert resolved == "abc1234"


def test_resolve_build_commit_preserves_existing_when_env_unset() -> None:
    mod = _hook_module()
    # No env override → the SHA baked into the sdist-shipped _build_info.py is preserved.
    assert mod._resolve_build_commit(Path("/no/git/here"), existing="baked77", env={}) == "baked77"


def test_resolve_build_commit_falls_back_to_none_outside_git() -> None:
    mod = _hook_module()
    # No env, no existing bake, and a path that is not a git tree → None (never raises).
    assert mod._resolve_build_commit(Path("/no/git/here"), existing=None, env={}) is None


def test_resolve_build_commit_raises_on_set_but_empty_env() -> None:
    mod = _hook_module()
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="set but empty"):
            mod._resolve_build_commit(
                Path("/anywhere"), existing="baked77", env={"REBAR_BUILD_COMMIT": blank}
            )


# ── AC3 (story 6168): the hook's fail-fast + git-fallback pinned by REAL builds ──
import ast  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import zipfile  # noqa: E402

_REPO_ROOT = Path(rebar.__file__).resolve().parents[2]


def _has_build() -> bool:
    try:
        import build  # noqa: F401

        return True
    except ModuleNotFoundError:
        return False


def _wheel_baked_commit(outdir: Path) -> str | None:
    wheels = list(outdir.glob("*.whl"))
    assert wheels, "no wheel produced"
    with zipfile.ZipFile(wheels[0]) as zf:
        name = next(n for n in zf.namelist() if n.endswith("rebar/_build_info.py"))
        # Parse the trivial `COMMIT = "..."` with ast (never exec — matches hatch_build.py).
        for node in ast.parse(zf.read(name).decode()).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "COMMIT" for t in node.targets
            ):
                v = node.value
                return v.value if isinstance(v, ast.Constant) else None
    return None


@pytest.mark.skipif(not _has_build(), reason="python -m build not available")
def test_hook_fails_when_rebar_build_commit_empty(tmp_path: Path) -> None:
    """AC3(a): `REBAR_BUILD_COMMIT=""` (set-but-empty, release context) fails the real build."""
    tree = tmp_path / "src"
    tree.mkdir()
    tar = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "archive", "HEAD"], capture_output=True, check=True
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(tree)], input=tar, check=True)
    import os

    env = {**os.environ, "REBAR_BUILD_COMMIT": ""}
    cp = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(tmp_path / "d"), str(tree)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert cp.returncode != 0, "an empty REBAR_BUILD_COMMIT must fail the build (release fail-fast)"


@pytest.mark.skipif(
    not _has_build() or shutil.which("git") is None, reason="python -m build / git not available"
)
def test_hook_falls_back_to_git_when_env_unset(tmp_path: Path) -> None:
    """AC3(b): with `REBAR_BUILD_COMMIT` UNSET, a build from a git checkout bakes the
    `git rev-parse --short HEAD` value (via the sdist's baked SHA preserved into the wheel)."""
    import os

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(_REPO_ROOT), str(clone)], check=True
    )
    short = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    env = {k: v for k, v in os.environ.items() if k != "REBAR_BUILD_COMMIT"}
    cp = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(tmp_path / "d"), str(clone)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert cp.returncode == 0, f"unset-env build must succeed (dev fallback): {cp.stderr[-1500:]}"
    assert _wheel_baked_commit(tmp_path / "d") == short, (
        "unset REBAR_BUILD_COMMIT must fall back to the git short SHA"
    )
