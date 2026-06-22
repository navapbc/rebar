"""Unit tests for rebar.grounding.deps — T0 dependency existence + abstain gauntlet.

Pins the load-bearing invariant: the deps lane STRUCTURALLY CANNOT emit a false
absence. A real package (registry 200) → ``refuted``; everything else (404,
transient 429/5xx/timeout/offline, stdlib, workspace member, import/dist mismatch,
unknown ecosystem) → ``abstain`` with a CLOSED reason — NEVER an asserted absence
(there is no "absent" outcome). Every emitted record validates against the schema.

The HTTP layer (``deps._http_get``) is the SOLE network seam and is monkeypatched
here — the unit tier is network-guarded, so no test touches the live network. One
optional live deps.dev integration check is gated behind ``@pytest.mark.external``.
"""

from __future__ import annotations

import urllib.error

import pytest

from rebar.grounding import deps
from rebar.grounding import evidence as ev

pytestmark = pytest.mark.unit


# ── HTTP stubbing helpers ─────────────────────────────────────────────────────


def _stub_status(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """Make the registry probe see HTTP ``code`` for every URL."""
    monkeypatch.setattr(deps, "_http_get", lambda url, timeout=10.0: code)


def _stub_raise(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    def _raise(url: str, timeout: float = 10.0) -> int:
        raise exc

    monkeypatch.setattr(deps, "_http_get", _raise)


def _ref(name: str, eco: str) -> dict[str, object]:
    return {"kind": "dependency", "name": name, "ecosystem": eco}


# ── the core three-valued contract ────────────────────────────────────────────


def test_real_package_200_refutes(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_status(monkeypatch, 200)
    rec = deps.refute_package(_ref("requests", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["provenance_tier"] == ev.TIER_T0
    assert rec["reference"]["name"] == "requests"
    assert rec["coverage"]["status"] == ev.STATUS_RAN
    ev.validate(rec)


def test_404_abstains_never_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_status(monkeypatch, 404)
    rec = deps.refute_package(_ref("reqeusts-not-real-xyz", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "private_or_internal_suspected"
    # NEVER a refuted-as-absence and there is no 'absent' outcome at all.
    assert rec["outcome"] != ev.OUTCOME_REFUTED
    assert rec["reason"] in ev.ABSTAIN_REASONS
    ev.validate(rec)


def test_410_also_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_status(monkeypatch, 410)
    rec = deps.refute_package(_ref("gone-pkg", "npm"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    ev.validate(rec)


# ── name normalization (PEP 503) ──────────────────────────────────────────────


def test_pep503_normalization_probes_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def _capture(url: str, timeout: float = 10.0) -> int:
        seen.append(url)
        return 200

    monkeypatch.setattr(deps, "_http_get", _capture)
    rec = deps.refute_package(_ref("scikit_learn", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    # Probed the PEP-503 canonical form, not the asserted spelling.
    assert "scikit-learn" in seen[0]
    assert "scikit_learn" not in seen[0]
    # But the asserted name is preserved on the reference.
    assert rec["reference"]["name"] == "scikit_learn"
    assert "normalized to 'scikit-learn'" in rec["detail"]
    ev.validate(rec)


def test_normalize_name_per_ecosystem() -> None:
    assert deps.normalize_name("pypi", "Foo.Bar_Baz") == "foo-bar-baz"
    assert deps.normalize_name("cargo", "Serde_JSON") == "serde_json"
    assert deps.normalize_name("npm", "@Scope/Name") == "@scope/name"
    # Go uppercase letters are !-escaped (deps.dev/Go proxy convention).
    assert deps.normalize_name("go", "github.com/Pkg/Errors") == "github.com/!pkg/!errors"
    assert deps.normalize_name("go", "github.com/pkg/errors") == "github.com/pkg/errors"


# ── stdlib / builtin gauntlet ─────────────────────────────────────────────────


def test_python_stdlib_abstains_with_stdlib_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even if the registry would 200, a stdlib name must short-circuit to abstain
    # BEFORE the probe — so make the probe explode to prove it's never called.
    _stub_raise(monkeypatch, AssertionError("probe must not run for stdlib"))
    rec = deps.refute_package(_ref("os", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == ev.DEFAULT_REASON
    assert "stdlib" in rec["detail"]
    ev.validate(rec)


def test_node_builtin_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_raise(monkeypatch, AssertionError("probe must not run for builtin"))
    rec = deps.refute_package(_ref("fs", "npm"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert "stdlib" in rec["detail"]
    ev.validate(rec)


def test_go_std_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_raise(monkeypatch, AssertionError("probe must not run for go std"))
    rec = deps.refute_package(_ref("fmt", "golang"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    ev.validate(rec)


def test_go_vanity_host_is_not_treated_as_std(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_status(monkeypatch, 200)
    rec = deps.refute_package(_ref("github.com/pkg/errors", "golang"))
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    ev.validate(rec)


# ── workspace / monorepo membership ───────────────────────────────────────────


def test_workspace_member_abstains_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_raise(monkeypatch, AssertionError("probe must not run for workspace member"))
    rec = deps.refute_package(
        _ref("my-internal-crate", "cargo"),
        workspace_members={"my-internal-crate"},
    )
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "private_or_internal_suspected"
    ev.validate(rec)


# ── import-name-vs-distribution-name mismatch ─────────────────────────────────


def test_import_dist_mismatch_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_raise(monkeypatch, AssertionError("probe must not run for known import alias"))
    rec = deps.refute_package(_ref("bs4", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "ambiguous"
    assert "beautifulsoup4" in rec["detail"]
    ev.validate(rec)


# ── transient / offline → abstain (never a false absence) ─────────────────────


def test_rate_limited_429_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_status(monkeypatch, 429)
    rec = deps.refute_package(_ref("requests", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "rate_limited"
    ev.validate(rec)


def test_server_error_5xx_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_status(monkeypatch, 503)
    rec = deps.refute_package(_ref("requests", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "network_error"
    ev.validate(rec)


def test_timeout_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_raise(monkeypatch, TimeoutError("read timed out"))
    rec = deps.refute_package(_ref("requests", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "timeout"
    ev.validate(rec)


def test_urlerror_with_timeout_reason_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_raise(monkeypatch, urllib.error.URLError(TimeoutError("timed out")))
    rec = deps.refute_package(_ref("requests", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "timeout"
    ev.validate(rec)


def test_offline_urlerror_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_raise(monkeypatch, urllib.error.URLError("Name or service not known"))
    rec = deps.refute_package(_ref("requests", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "network_error"
    ev.validate(rec)


def test_oserror_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_raise(monkeypatch, OSError("connection reset"))
    rec = deps.refute_package(_ref("requests", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "network_error"
    ev.validate(rec)


def test_unexpected_status_abstains_other(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_status(monkeypatch, 403)
    rec = deps.refute_package(_ref("requests", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "other"
    ev.validate(rec)


# ── unknown / unsupported ecosystem ───────────────────────────────────────────


def test_unknown_ecosystem_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_raise(monkeypatch, AssertionError("probe must not run for unknown eco"))
    rec = deps.refute_package(_ref("whatever", "cocoapods"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "unsupported_lang"
    ev.validate(rec)


def test_empty_name_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = deps.refute_package(_ref("", "pypi"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "ambiguous"
    ev.validate(rec)


def test_gem_has_no_oracle_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_raise(monkeypatch, AssertionError("probe must not run for gem (no oracle)"))
    rec = deps.refute_package(_ref("rails", "gem"))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "unsupported_lang"
    ev.validate(rec)


# ── polyglot batch + zero-false-absent invariant ──────────────────────────────


def test_polyglot_batch_never_false_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mixed verdicts: real (200) refutes, hallucinated (404) abstains. The
    # critical property: not a single record is a refuted-as-absence, and the only
    # 'refuted' records are the ones the registry confirmed exist.
    def _router(url: str, timeout: float = 10.0) -> int:
        # Exact-segment match: only the two real packages 200; slop names 404.
        return 200 if url.endswith("/react") or url.endswith("/serde") else 404

    monkeypatch.setattr(deps, "_http_get", _router)
    refs = [
        _ref("react", "npm"),
        _ref("serde", "cargo"),
        _ref("reactt-not-real-xyz", "npm"),
        _ref("serde-fake-xyz-9000", "cargo"),
    ]
    recs = deps.refute_packages(refs)
    assert all(r["outcome"] in (ev.OUTCOME_REFUTED, ev.OUTCOME_ABSTAIN) for r in recs)
    refuted = [r for r in recs if r["outcome"] == ev.OUTCOME_REFUTED]
    abstained = [r for r in recs if r["outcome"] == ev.OUTCOME_ABSTAIN]
    assert len(refuted) == 2
    assert len(abstained) == 2
    for r in recs:
        ev.validate(r)


# ── manifest / lockfile enumeration ───────────────────────────────────────────


def test_enumerate_pyproject(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "demo"
dependencies = ["requests>=2.0", "PyYAML", "rich ; python_version>='3.8'"]
[project.optional-dependencies]
dev = ["pytest>=7"]
""",
        encoding="utf-8",
    )
    res = deps.enumerate_dependencies(tmp_path)
    names = {r["name"] for r in res["references"]}
    assert {"requests", "PyYAML", "rich", "pytest"} <= names
    assert all(r["ecosystem"] == "pypi" for r in res["references"])
    assert not res["errors"]


def test_enumerate_requirements_txt(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "# a comment\nrequests==2.31.0\nflask\n-r other.txt\n\nnumpy>=1.0  # inline\n",
        encoding="utf-8",
    )
    res = deps.enumerate_dependencies(tmp_path)
    names = {r["name"] for r in res["references"]}
    assert names == {"requests", "flask", "numpy"}


def test_enumerate_package_json_and_workspace(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"name": "monorepo-root", "dependencies": {"react": "^18"}, '
        '"devDependencies": {"jest": "^29"}, "workspaces": ["packages/*"]}',
        encoding="utf-8",
    )
    res = deps.enumerate_dependencies(tmp_path)
    names = {r["name"] for r in res["references"]}
    assert {"react", "jest"} <= names
    assert "monorepo-root" in res["workspace_members"]


def test_enumerate_cargo_toml(tmp_path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        """
[package]
name = "root-crate"
[dependencies]
serde = "1.0"
local-dep = { path = "../local" }
[workspace]
members = ["crates/foo", "crates/bar"]
""",
        encoding="utf-8",
    )
    res = deps.enumerate_dependencies(tmp_path)
    names = {r["name"] for r in res["references"]}
    assert "serde" in names
    assert "local-dep" not in names  # path dep skipped
    assert {"foo", "bar", "root-crate"} <= res["workspace_members"]


def test_enumerate_go_mod_with_replace(tmp_path) -> None:
    (tmp_path / "go.mod").write_text(
        """
module example.com/app

go 1.21

require (
	github.com/pkg/errors v0.9.1
	github.com/spf13/cobra v1.8.0 // indirect
)

require github.com/stretchr/testify v1.8.0

replace example.com/internal => ./internal
""",
        encoding="utf-8",
    )
    res = deps.enumerate_dependencies(tmp_path)
    names = {r["name"] for r in res["references"]}
    assert {"github.com/pkg/errors", "github.com/spf13/cobra", "github.com/stretchr/testify"} <= names
    assert "example.com/internal" in res["workspace_members"]


def test_enumerate_pom_xml(tmp_path) -> None:
    (tmp_path / "pom.xml").write_text(
        """<?xml version="1.0"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modules><module>sub-a</module></modules>
  <dependencies>
    <dependency><groupId>com.google.guava</groupId><artifactId>guava</artifactId></dependency>
  </dependencies>
</project>
""",
        encoding="utf-8",
    )
    res = deps.enumerate_dependencies(tmp_path)
    names = {r["name"] for r in res["references"]}
    assert "com.google.guava:guava" in names
    assert "sub-a" in res["workspace_members"]


def test_enumerate_gemfile(tmp_path) -> None:
    (tmp_path / "Gemfile").write_text(
        "source 'https://rubygems.org'\ngem 'rails', '~> 7.0'\n# gem 'commented'\ngem \"puma\"\n",
        encoding="utf-8",
    )
    res = deps.enumerate_dependencies(tmp_path)
    names = {r["name"] for r in res["references"]}
    assert names == {"rails", "puma"}
    assert all(r["ecosystem"] == "gem" for r in res["references"])


def test_enumerate_malformed_manifest_fails_open(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{not valid json", encoding="utf-8")
    res = deps.enumerate_dependencies(tmp_path)
    # No raise; the bad file is recorded and enumeration continues.
    assert res["references"] == []
    assert any(e["file"] == "package.json" for e in res["errors"])


def test_enumerate_empty_dir(tmp_path) -> None:
    res = deps.enumerate_dependencies(tmp_path)
    assert res == {"references": [], "workspace_members": set(), "errors": []}


def test_enumeration_feeds_refutation_workspace_guard(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # End-to-end: a Cargo workspace member enumerated as internal must abstain even
    # if the registry would 200 — the workspace guard wins.
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "foo"\n[workspace]\nmembers = ["crates/foo"]\n[dependencies]\nserde = "1"\n',
        encoding="utf-8",
    )
    res = deps.enumerate_dependencies(tmp_path)
    members = res["workspace_members"]
    _stub_status(monkeypatch, 200)
    internal = deps.refute_package(_ref("foo", "cargo"), workspace_members=members)
    assert internal["outcome"] == ev.OUTCOME_ABSTAIN
    assert internal["reason"] == "private_or_internal_suspected"
    public = deps.refute_package(_ref("serde", "cargo"), workspace_members=members)
    assert public["outcome"] == ev.OUTCOME_REFUTED
