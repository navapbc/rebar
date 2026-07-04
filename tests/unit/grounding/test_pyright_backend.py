"""Fail-open + confirm-only matrix for the pyright T2 backend (epic 850f, story S3).

pyright is not assumed installed, so every case drives the backend through a
monkeypatched ``_run_pyright`` returning either a fail-open ``RunResult`` or a
captured-real ``pyright --outputjson`` payload. The one invariant under test: the
backend returns ONLY ``refuted`` (a trustworthy resolve) or ``abstain`` (a closed
reason) — it never asserts an absence.
"""

from __future__ import annotations

import json

import pytest

from rebar.grounding import evidence as ev
from rebar.grounding import harness
from rebar.grounding import pyright_backend as pb

pytestmark = pytest.mark.unit

_FILE = "pkg/mod.py"
_MEMBER = {
    "kind": "member",
    "name": "store.reconcile_tickets",
    "in_file": _FILE,
    "language": "python",
}


def _payload(diagnostics, repo_root):
    """A pyright --outputjson payload with diagnostics anchored to the abs file path."""
    import os

    abs_file = os.path.join(repo_root, _FILE)
    return {
        "version": "1.1.400",
        "generalDiagnostics": [{**d, "file": d.get("file", abs_file)} for d in diagnostics],
        "summary": {"errorCount": len(diagnostics)},
    }


def _ran(stdout: str) -> harness.RunResult:
    return harness.RunResult(backend="pyright", completed=True, returncode=0, stdout=stdout)


def _patch_run(monkeypatch, result):
    monkeypatch.setattr(pb, "_run_pyright", lambda repo_root, timeout: result)


def _patch_payload(monkeypatch, diagnostics):
    def fake(repo_root, timeout):
        return _ran(json.dumps(_payload(diagnostics, repo_root)))

    monkeypatch.setattr(pb, "_run_pyright", fake)
    # keep the refuted-path version() probe cheap + offline
    monkeypatch.setattr(pb, "version", lambda: "1.1.400")


# ── the trustworthy resolve → refuted@T2 ──────────────────────────────────────


def test_clean_resolve_refutes(monkeypatch, tmp_path) -> None:
    _patch_payload(monkeypatch, [])  # no diagnostics at all → resolves
    rec = pb.refute(_MEMBER, repo_root=str(tmp_path))
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["provenance_tier"] == ev.TIER_T2
    ev.validate(rec)


def test_unrelated_diagnostic_elsewhere_still_refutes(monkeypatch, tmp_path) -> None:
    # a diagnostic in a DIFFERENT file must not block the resolve
    def fake(repo_root, timeout):
        payload = _payload([], str(tmp_path))
        payload["generalDiagnostics"].append(
            {
                "file": "/other/file.py",
                "rule": "reportAttributeAccessIssue",
                "message": 'Cannot access attribute "reconcile_tickets"',
            }
        )
        return _ran(json.dumps(payload))

    monkeypatch.setattr(pb, "_run_pyright", fake)
    monkeypatch.setattr(pb, "version", lambda: "1.1.400")
    rec = pb.refute(_MEMBER, repo_root=str(tmp_path))
    assert rec["outcome"] == ev.OUTCOME_REFUTED


# ── suspected-absent → abstain (never an asserted absence) ────────────────────


def test_unresolved_attribute_abstains(monkeypatch, tmp_path) -> None:
    _patch_payload(
        monkeypatch,
        [
            {
                "rule": "reportAttributeAccessIssue",
                "message": 'Cannot access attribute "reconcile_tickets" for class "TicketStore"',
            }
        ],
    )
    rec = pb.refute(_MEMBER, repo_root=str(tmp_path))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "other"
    assert rec["provenance_tier"] == ev.TIER_T2


def test_unrecognized_diag_at_reference_abstains(monkeypatch, tmp_path) -> None:
    # a diagnostic names the leaf but with a rule we don't recognize → fail-safe abstain
    _patch_payload(
        monkeypatch,
        [
            {
                "rule": "reportArgumentType",
                "message": 'Argument to "reconcile_tickets" is wrong type',
            }
        ],
    )
    rec = pb.refute(_MEMBER, repo_root=str(tmp_path))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "other"


def test_env_not_built_abstains(monkeypatch, tmp_path) -> None:
    # unresolved imports in the file → environment not built → abstain (don't trust)
    _patch_payload(
        monkeypatch,
        [{"rule": "reportMissingImports", "message": 'Import "django" could not be resolved'}],
    )
    rec = pb.refute(_MEMBER, repo_root=str(tmp_path))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "other"
    assert "environment not built" in rec["detail"]


# ── fail-open matrix ──────────────────────────────────────────────────────────


def test_no_tool_abstains(monkeypatch, tmp_path) -> None:
    _patch_run(
        monkeypatch,
        harness.RunResult(backend="pyright", completed=False, abstain_reason="no_tool"),
    )
    rec = pb.refute(_MEMBER, repo_root=str(tmp_path))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "no_tool"


def test_timeout_abstains(monkeypatch, tmp_path) -> None:
    _patch_run(
        monkeypatch,
        harness.RunResult(backend="pyright", completed=False, abstain_reason="timeout"),
    )
    rec = pb.refute(_MEMBER, repo_root=str(tmp_path))
    assert rec["reason"] == "timeout"


def test_crash_abstains(monkeypatch, tmp_path) -> None:
    _patch_run(
        monkeypatch,
        harness.RunResult(
            backend="pyright", completed=False, abstain_reason="other", detail="spawn failed"
        ),
    )
    rec = pb.refute(_MEMBER, repo_root=str(tmp_path))
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "other"


def test_unparseable_output_abstains(monkeypatch, tmp_path) -> None:
    _patch_run(monkeypatch, _ran("not json {{{"))
    rec = pb.refute(_MEMBER, repo_root=str(tmp_path))
    assert rec["reason"] == "parse_error"


def test_non_python_reference_abstains(tmp_path) -> None:
    rec = pb.refute(
        {"kind": "member", "name": "obj.field", "in_file": "main.go", "language": "go"},
        repo_root=str(tmp_path),
    )
    assert rec["reason"] == "unsupported_lang"


def test_no_in_file_abstains(tmp_path) -> None:
    rec = pb.refute({"kind": "member", "name": "a.b"}, repo_root=str(tmp_path))
    assert rec["reason"] == "ambiguous"


def test_no_name_abstains(tmp_path) -> None:
    rec = pb.refute({"kind": "member", "in_file": _FILE}, repo_root=str(tmp_path))
    assert rec["reason"] == "ambiguous"


# ── the confirm-only invariant, asserted across the whole matrix ──────────────


def test_backend_only_ever_refutes_or_abstains(monkeypatch, tmp_path) -> None:
    scenarios = [
        lambda: _patch_payload(monkeypatch, []),
        lambda: _patch_payload(
            monkeypatch,
            [{"rule": "reportAttributeAccessIssue", "message": '"reconcile_tickets"'}],
        ),
        lambda: _patch_payload(monkeypatch, [{"rule": "reportMissingImports", "message": "x"}]),
        lambda: _patch_run(
            monkeypatch,
            harness.RunResult(backend="pyright", completed=False, abstain_reason="timeout"),
        ),
        lambda: _patch_run(monkeypatch, _ran("garbage")),
    ]
    for setup in scenarios:
        setup()
        rec = pb.refute(_MEMBER, repo_root=str(tmp_path))
        assert rec["outcome"] in (ev.OUTCOME_REFUTED, ev.OUTCOME_ABSTAIN)
        assert rec["outcome"] != "asserted_absent"  # the outcome vocabulary has no such value


# ── the per-project-root cache ────────────────────────────────────────────────


def test_cache_reuses_one_invocation(monkeypatch, tmp_path) -> None:
    calls = {"n": 0}

    def fake(repo_root, timeout):
        calls["n"] += 1
        return _ran(json.dumps(_payload([], repo_root)))

    monkeypatch.setattr(pb, "_run_pyright", fake)
    monkeypatch.setattr(pb, "version", lambda: "1.1.400")
    cache: dict = {}
    ref2 = {**_MEMBER, "name": "store.other_method"}
    pb.refute(_MEMBER, repo_root=str(tmp_path), cache=cache)
    pb.refute(ref2, repo_root=str(tmp_path), cache=cache)
    assert calls["n"] == 1  # second reference reused the cached pyright run
