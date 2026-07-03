"""WS5 (epic b744): secrets (gitleaks) + High-Critical security (semgrep/opengrep) detectors
feeding the code-review gate's CONSUMER-side fail-CLOSED block.

Pins: the detectors register on the right backends; the SARIF sentinel dispatches + abstains
fail-OPEN when gitleaks is absent; the verdict assembly fail-CLOSES (force BLOCK + coverage-gap)
on an abstain (RED fixture) OR a match; matches are diff-scoped; and the ORACLE itself never
blocks (stays fail-open).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# ── detectors register on the right backends ───────────────────────────────────────────────
def test_security_detectors_register_on_sarif_and_opengrep():
    from rebar.grounding.detectors import BACKEND_OPENGREP, BACKEND_SARIF, load_registry

    sec = [d for d in load_registry() if d.id.startswith("rebar.builtin.security.")]
    by_id = {d.id: d for d in sec}
    assert by_id["rebar.builtin.security.secrets-gitleaks"].backend == BACKEND_SARIF
    opengrep = [d for d in sec if d.backend == BACKEND_OPENGREP]
    assert len(opengrep) >= 1 and all(
        d.languages for d in opengrep
    )  # the semgrep High/Critical rules


def test_security_rule_yaml_files_are_present_and_nonempty():
    # the vendored-rules freshness anchor: a botched `make vendor-security-rules` that empties the
    # subset is caught (the rules must exist + declare the rebar.builtin.security.* ids).
    d = Path("src/rebar/grounding/detectors/builtin")
    assert (d / "security_secrets_gitleaks.yaml").read_text().strip()
    body = (d / "security_owasp_cwe.yaml").read_text()
    assert "rebar.builtin.security." in body and "rules:" in body


# ── the SARIF backend: dispatch + fail-OPEN abstain ──────────────────────────────────────────
def test_sarif_sentinel_abstains_fail_open_when_gitleaks_absent(monkeypatch):
    from rebar.grounding import engine_b
    from rebar.grounding.detectors import load_registry

    sentinel = next(d for d in load_registry() if d.id == "rebar.builtin.security.secrets-gitleaks")
    monkeypatch.setattr(engine_b, "_resolve_binary", lambda candidates: None)  # gitleaks absent
    with tempfile.TemporaryDirectory() as t:
        recs = engine_b._run_sarif([sentinel], Path(t))
    assert len(recs) == 1
    assert recs[0]["outcome"] == "abstain" and recs[0]["reason"] == "no_tool"


def test_oracle_scan_never_blocks_only_abstains(monkeypatch):
    # The cardinal invariant: Engine B is fail-OPEN. With no secret tool, the sarif sentinel
    # ABSTAINS — the ScanResult carries an abstain, NEVER a 'block' outcome (block is the
    # consumer's job, not the oracle's).
    from rebar.grounding import engine_b
    from rebar.grounding.detectors import Registry, load_registry

    monkeypatch.setattr(engine_b, "_resolve_binary", lambda candidates: None)
    sentinel = tuple(
        d for d in load_registry() if d.id == "rebar.builtin.security.secrets-gitleaks"
    )
    with tempfile.TemporaryDirectory() as t:
        res = engine_b.scan(t, registry=Registry(detectors=sentinel))
    assert res.abstains()  # fail-open abstain present
    assert all(r.get("outcome") != "block" for r in res.records)  # the oracle never blocks


# ── the consumer fail-CLOSED (verdict assembly) ──────────────────────────────────────────────
def _pass_verdict() -> dict:
    return {"verdict": "PASS", "blocking": [], "advisory": [], "coaching": [], "coverage": {}}


def test_failclosed_forces_block_on_detector_abstain(monkeypatch):
    # RED FIXTURE: an unavailable/errored secrets detector abstains → the gate MUST fail-closed
    # (BLOCK + a coverage-gap annotation), never silently pass.
    from rebar.llm.code_review import detectors

    monkeypatch.setattr(
        detectors,
        "run_security_detectors",
        lambda **kw: {"secret-detection": {"abstained": [{"reason": "no_tool"}], "matches": []}},
    )
    v = detectors.apply_failclosed(_pass_verdict(), changed_files=["app.py"], repo_root=None)
    assert v["verdict"] == "BLOCK"
    note = v["coverage"]["security_detectors"][0]
    assert note["criterion"] == "secret-detection" and note["reason"] == "fail-closed-abstain"
    assert "no_tool" in note["abstain_reasons"]


def test_failclosed_forces_block_on_detector_match(monkeypatch):
    from rebar.llm.code_review import detectors

    monkeypatch.setattr(
        detectors,
        "run_security_detectors",
        lambda **kw: {
            "high-critical-security": {
                "abstained": [],
                "matches": [
                    {
                        "detector_id": "rebar.builtin.security.python-eval-exec-injection",
                        "location": {"file": "app.py"},
                    }
                ],
            }
        },
    )
    v = detectors.apply_failclosed(_pass_verdict(), changed_files=["app.py"], repo_root=None)
    assert v["verdict"] == "BLOCK"
    note = v["coverage"]["security_detectors"][0]
    assert note["reason"] == "detector-finding"  # distinct from a fail-closed abstain
    # A forced-BLOCK must NAME the match in `blocking` — else the review-bot adapter renders
    # "found 0 blocking issue(s)" with no finding, hiding a real match from the author (bug f367).
    assert v["blocking"], "a match-forced BLOCK must have a non-empty blocking list"
    entry = v["blocking"][0]
    assert entry["criteria"] == ["high-critical-security"]  # the criterion is named
    assert entry["severity"] == "critical" and entry["decision"] == "block"
    assert "high-critical-security" in entry["finding"] and "app.py" in entry["finding"]
    assert entry["location"] == "app.py"  # the matched changed file


def test_failclosed_match_names_no_blocking_when_criterion_advisory(monkeypatch):
    # A detector criterion whose `blocking_enabled` is False records the match in coverage but must
    # NOT force BLOCK nor append a blocking finding (advisory-only stays advisory).
    from rebar.llm.code_review import detectors, registry

    monkeypatch.setattr(
        detectors,
        "run_security_detectors",
        lambda **kw: {
            "high-critical-security": {
                "abstained": [],
                "matches": [{"detector_id": "rebar.builtin.security.python-eval-exec-injection"}],
            }
        },
    )
    monkeypatch.setattr(registry, "threshold_for", lambda crits: (0.95, False))  # not blocking
    v = detectors.apply_failclosed(_pass_verdict(), changed_files=["app.py"], repo_root=None)
    assert v["verdict"] == "PASS"  # advisory-only detector never forces BLOCK
    assert v["blocking"] == []  # nothing appended
    assert v["coverage"]["security_detectors"][0]["reason"] == "detector-finding"


def test_failclosed_is_a_noop_when_no_security_signal(monkeypatch):
    from rebar.llm.code_review import detectors

    monkeypatch.setattr(detectors, "run_security_detectors", lambda **kw: {})
    v = detectors.apply_failclosed(_pass_verdict(), changed_files=["app.py"], repo_root=None)
    assert v["verdict"] == "PASS"  # the oracle's fail-open posture is untouched
    assert "security_detectors" not in v["coverage"]


# ── diff-scope: a match on an UNCHANGED file is filtered out ────────────────────────────────
def test_matches_are_diff_scoped_to_changed_files(monkeypatch):
    from rebar.llm.code_review import detectors

    # a security MATCH on other.py (not changed) must NOT count; one on app.py must.
    class _Res:
        records = (
            {
                "detector_id": "rebar.builtin.security.python-eval-exec-injection",
                "outcome": "match",
                "location": {"file": "other.py"},
            },
            {
                "detector_id": "rebar.builtin.security.python-eval-exec-injection",
                "outcome": "match",
                "location": {"file": "app.py"},
            },
        )

    monkeypatch.setattr("rebar.grounding.engine_b.scan", lambda *a, **k: _Res())
    out = detectors.run_security_detectors(changed_files=["app.py"], repo_root=None)
    matches = out["high-critical-security"]["matches"]
    assert len(matches) == 1 and matches[0]["location"]["file"] == "app.py"


def test_empty_changed_files_keeps_no_matches(monkeypatch):
    # diff-scope: with NO changed files (empty diff), a repo-wide match must NOT count (it is
    # not part of the change) — guards against over-blocking on a pre-existing repo secret.
    from rebar.llm.code_review import detectors

    class _Res:
        records = (
            {
                "detector_id": "rebar.builtin.security.secrets-gitleaks",
                "outcome": "match",
                "location": {"file": "preexisting.py"},
            },
        )

    monkeypatch.setattr("rebar.grounding.engine_b.scan", lambda *a, **k: _Res())
    out = detectors.run_security_detectors(changed_files=[], repo_root=None)
    assert out.get("secret-detection", {}).get("matches", []) == []


# ── _run_sarif: the REAL chain (report-read / abstain-on-failure / re-attribute / relativize) ─
def _write_sarif(path, *, uri, rule_id="github-pat"):
    import json

    Path(path).write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "tool": {"driver": {"name": "gitleaks"}},
                        "results": [
                            {
                                "ruleId": rule_id,
                                "message": {"text": "secret"},
                                "locations": [
                                    {"physicalLocation": {"artifactLocation": {"uri": uri}}}
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )


def test_run_sarif_reattributes_and_relativizes(monkeypatch):
    # Deterministic (mocked subprocess): a gitleaks SARIF with its OWN ruleId + an ABSOLUTE uri
    # must come back attributed to the SENTINEL id and relativized to repo_root — else the
    # consumer's id-prefix filter + diff-scope drop every real secret.
    from rebar.grounding import engine_b, harness
    from rebar.grounding.detectors import load_registry

    sentinel = next(d for d in load_registry() if d.id == "rebar.builtin.security.secrets-gitleaks")
    with tempfile.TemporaryDirectory() as t:
        repo = Path(t)
        monkeypatch.setattr(engine_b, "_resolve_binary", lambda c: "/usr/bin/gitleaks")
        monkeypatch.setattr(engine_b, "_binary_version", lambda b: "8.30.1")

        def fake_run_tool(cmd, *, backend, version=None):
            rp = cmd[cmd.index("--report-path") + 1]
            _write_sarif(rp, uri=str(repo / "app.py"))  # ABSOLUTE uri, gitleaks ruleId
            return harness.RunResult(backend=backend, completed=True, returncode=0, version=version)

        monkeypatch.setattr(harness, "run_tool", fake_run_tool)
        recs = engine_b._run_sarif([sentinel], repo)
    assert len(recs) == 1
    assert recs[0]["outcome"] == "match"
    assert recs[0]["detector_id"] == sentinel.id  # re-attributed (not "github-pat")
    assert recs[0]["location"]["file"] == "app.py"  # relativized (not the absolute path)


def test_run_sarif_abstains_when_no_parseable_report(monkeypatch):
    # The silent-PASS guard: gitleaks "ran" (non-zero, wrote nothing) → ABSTAIN, never 0 records.
    from rebar.grounding import engine_b, harness
    from rebar.grounding.detectors import load_registry

    sentinel = next(d for d in load_registry() if d.id == "rebar.builtin.security.secrets-gitleaks")
    monkeypatch.setattr(engine_b, "_resolve_binary", lambda c: "/usr/bin/gitleaks")
    monkeypatch.setattr(engine_b, "_binary_version", lambda b: "8.30.1")
    # run_tool returns "completed, non-zero, NO report written" (the /dev/stdout-style failure).
    monkeypatch.setattr(
        harness,
        "run_tool",
        lambda cmd, **k: harness.RunResult(backend="sarif", completed=True, returncode=1),
    )
    with tempfile.TemporaryDirectory() as t:
        recs = engine_b._run_sarif([sentinel], Path(t))
    assert len(recs) == 1 and recs[0]["outcome"] == "abstain"


@pytest.mark.skipif(__import__("shutil").which("gitleaks") is None, reason="gitleaks not installed")
def test_planted_secret_blocks_end_to_end_real_gitleaks():
    # NON-mocked wiring/smoke test: a planted secret in a changed file flows real gitleaks → SARIF →
    # re-attribute → relativize → diff-scope → fail-closed BLOCK. (The headline WS5 criterion; the
    # parse/decision logic is covered deterministically by the mocked _run_sarif tests above.)
    #
    # ASSEMBLE the fake PAT at RUNTIME — never commit a contiguous secret literal. This is exactly
    # how gitleaks tests its own github-pat rule: `ghp_` + 36 random alphanumerics clears the
    # entropy floor and matches `ghp_[0-9a-zA-Z]{36}`, so gitleaks detects it; but because the token
    # is built in memory (and has an invalid CRC32 checksum), GitHub push-protection never sees a
    # secret in this source file. See docs/adr/0012 + the WS5 research note.
    import secrets as _secrets
    import string as _string

    from rebar.llm.code_review import detectors

    fake_pat = "ghp_" + "".join(
        _secrets.choice(_string.ascii_letters + _string.digits) for _ in range(36)
    )  # gitleaks:allow — assembled at runtime, no committed literal
    with tempfile.TemporaryDirectory() as t:
        Path(t, "app.py").write_text(f'GH = "{fake_pat}"\n')
        out = detectors.run_security_detectors(changed_files=["app.py"], repo_root=t)
        matches = out.get("secret-detection", {}).get("matches", [])
        assert matches, "real gitleaks should surface the planted secret"
        assert all(m["location"]["file"] == "app.py" for m in matches)
        v = detectors.apply_failclosed(_pass_verdict(), changed_files=["app.py"], repo_root=t)
    assert v["verdict"] == "BLOCK"


@pytest.mark.skipif(__import__("shutil").which("gitleaks") is None, reason="gitleaks not installed")
def test_repo_gitleaks_config_allowlists_doc_throwaway_but_not_real_secret():
    # Regression (ticket a6a5-f4a0-c5e6-40a8): the committed repo-root `.gitleaks.toml` allowlist
    # must SUPPRESS the documented non-secret Langfuse throwaway (`sk-lf-1234567890`, the local
    # docker-compose init default mirrored in docs/llm-framework.md) so a pure docs edit is not
    # false-positive fail-closed BLOCKed — WITHOUT weakening real-secret detection: a genuine
    # planted secret in the SAME scan must still surface. Auto-discovery of the root config is the
    # exact mechanism the detector relies on (engine_b._run_sarif runs `gitleaks detect --source
    # <repo_root>` with no `--config`).
    import secrets as _secrets
    import shutil as _shutil
    import string as _string

    from rebar.llm.code_review import detectors

    repo_config = Path(__file__).resolve().parents[2] / ".gitleaks.toml"
    assert repo_config.is_file(), "repo-root .gitleaks.toml must exist for the allowlist to apply"

    # A genuine secret assembled at RUNTIME (no committed literal). The throwaway is a committed
    # literal but is itself allowlisted by the config under test (so this file stays clean too).
    fake_pat = "ghp_" + "".join(
        _secrets.choice(_string.ascii_letters + _string.digits) for _ in range(36)
    )  # gitleaks:allow — assembled at runtime, no committed literal
    throwaway_line = "export LANGFUSE_SECRET_KEY=sk-lf-1234567890\n"  # gitleaks:allow

    def flagged_files(*, with_config: bool) -> set[str]:
        with tempfile.TemporaryDirectory() as t:
            if with_config:
                # Auto-discovered by gitleaks from the --source root (the detector's real path).
                _shutil.copy(repo_config, Path(t, ".gitleaks.toml"))
            Path(t, "doc_example.md").write_text(throwaway_line)
            Path(t, "app.py").write_text(f'GH = "{fake_pat}"\n')
            out = detectors.run_security_detectors(
                changed_files=["doc_example.md", "app.py"], repo_root=t
            )
            matches = out.get("secret-detection", {}).get("matches", [])
            return {m["location"]["file"] for m in matches}

    # Baseline (default gitleaks, no repo allowlist): the throwaway DOES trip — i.e. it is a real
    # false positive absent the allowlist (and the genuine secret trips, as always).
    baseline = flagged_files(with_config=False)
    assert "doc_example.md" in baseline, "the throwaway must trip WITHOUT the allowlist (real FP)"
    assert "app.py" in baseline

    # With the committed repo config: throwaway allowlisted, genuine secret still detected.
    with_cfg = flagged_files(with_config=True)
    assert "doc_example.md" not in with_cfg, "documented throwaway must be allowlisted (no FP)"
    assert "app.py" in with_cfg, "a genuine secret must still be detected (fail-closed preserved)"


# ── grounding_info advertises the new backend ───────────────────────────────────────────────
def test_grounding_info_advertises_the_sarif_backend():
    from rebar.grounding import oracle
    from rebar.grounding.detectors import BACKENDS

    advertised = {b["name"] for b in oracle._backend_availability()}
    assert "sarif" in advertised
    assert BACKENDS <= advertised  # every Engine B backend is advertised


# ── vendored-rules CI freshness gate (the make-target's CI companion) ────────────────────────
def test_security_rules_pin_is_present_and_well_formed():
    from rebar.grounding.detectors import security_pin

    pin = security_pin.load_pin()
    assert "vendored_at" in pin and isinstance(pin["vendored_at"], str)
    assert int(pin["cadence_days"]) > 0
    assert pin["families"]  # the pinned families are recorded
    # the recorded date must parse — the gate relies on it
    import datetime as _dt

    _dt.date.fromisoformat(pin["vendored_at"])


def test_freshness_fresh_pin_is_not_stale_and_emits_no_warning():
    import datetime as _dt

    from rebar.grounding.detectors import security_pin

    pin = {"vendored_at": "2026-01-01", "cadence_days": 90, "families": ["x"]}
    status = security_pin.freshness(_dt.date(2026, 2, 1), pin=pin)  # 31d < 90d
    assert status["stale"] is False and status["age_days"] == 31
    assert security_pin.format_warning(status) is None


def test_freshness_stale_pin_warns():
    import datetime as _dt

    from rebar.grounding.detectors import security_pin

    pin = {"vendored_at": "2026-01-01", "cadence_days": 90, "families": ["x"]}
    status = security_pin.freshness(_dt.date(2026, 6, 1), pin=pin)  # 151d > 90d
    assert status["stale"] is True
    warning = security_pin.format_warning(status)
    assert warning and warning.startswith("::warning") and "vendor-security-rules" in warning


def test_freshness_unparseable_pin_is_treated_as_stale():
    # fail-toward-refresh: a missing/garbled vendored_at must NOT silently pass as fresh.
    import datetime as _dt

    from rebar.grounding.detectors import security_pin

    status = security_pin.freshness(_dt.date(2026, 6, 1), pin={"vendored_at": "not-a-date"})
    assert status["stale"] is True and status["age_days"] is None
    assert "unparseable" in security_pin.format_warning(status)


def test_freshness_gate_main_is_warn_only_exit_zero(capsys):
    # The committed pin is fresh today, but main() must ALWAYS exit 0 (warn-only per the AC),
    # whether or not it is stale.
    from rebar.grounding.detectors import security_pin

    assert security_pin.main() == 0
    out = capsys.readouterr().out
    assert "freshness" in out.lower() or "::warning" in out
