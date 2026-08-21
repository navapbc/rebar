"""Tests for the shrink-only function-complexity baseline gate (story c9f7).

The gate lives in ``scripts/check_complexity_baseline.py`` and freezes the current
production ``C901`` (McCabe) debt in ``.github/complexity-baseline.json`` as a per-symbol
ceiling, failing only on NEW or INCREASED complexity while allowing reductions/removals.

Every census count is derived LIVE by running Ruff against the current tree — no census
number is hardcoded.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest
import tomllib
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_complexity_baseline.py"
BASELINE_PATH = REPO_ROOT / ".github" / "complexity-baseline.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "_build-and-test.yml"


def _load():
    spec = importlib.util.spec_from_file_location("check_complexity_baseline", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ccb = _load()


# ─────────────────────────── live-census helpers ────────────────────────────


def _live_census() -> list[dict]:
    """Run the raw JSON census (command 5: no ``--exit-zero``) and return findings.

    Exit code is asserted to be 1 by the caller when it needs it; here we only parse.
    """
    proc = subprocess.run(
        [
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "C901",
            "--config",
            "lint.mccabe.max-complexity=15",
            "--output-format=json",
            "src/rebar",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout), proc.returncode


def _live_count() -> int:
    findings, _ = _live_census()
    return len(findings)


# ───────────────────────── census / exit-code contract ──────────────────────


def test_raw_json_census_exits_1_with_live_count():
    """Command 5: raw JSON census (no --exit-zero) exits 1 and length == live count."""
    findings, rc = _live_census()
    assert rc == 1, "raw JSON census must exit 1 because findings exist"
    assert all(f["code"] == "C901" for f in findings)
    assert len(findings) == _live_count()
    assert len(findings) > 0


def test_census_transport_exit_zero_same_count():
    """Command 2: --exit-zero census exits 0 with the same live census count."""
    proc = subprocess.run(
        [
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "C901",
            "--config",
            "lint.mccabe.max-complexity=15",
            "--exit-zero",
            "--output-format=json",
            "src/rebar",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    findings = json.loads(proc.stdout)
    raw, raw_rc = _live_census()
    assert raw_rc == 1
    assert len(findings) == len(raw)


def test_wrapper_check_on_committed_baseline():
    """Command 3: --check on the committed tree exits 0 with the shrink-only verdict.

    The gate's contract is ``new=0`` and ``increased=0`` — ``active``/``stale`` are
    informational. A contributor who legitimately REDUCES a baselined function makes
    ``stale`` nonzero (and drops that symbol out of ``active``) and must still pass.
    """
    live = _live_count()
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    match = re.fullmatch(r"active=(\d+) new=(\d+) increased=(\d+) stale=(\d+)", proc.stdout.strip())
    assert match is not None, f"unexpected verdict line: {proc.stdout!r}"
    active, new, increased, _stale = (int(g) for g in match.groups())
    assert new == 0
    assert increased == 0
    # Only a live finding sitting exactly at its ceiling can be `active`.
    assert active <= live


# ─────────────────────────── pyproject / baseline ───────────────────────────


def test_pyproject_mccabe_threshold_is_15():
    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    mccabe = data["tool"]["ruff"]["lint"]["mccabe"]
    assert mccabe["max-complexity"] == 15
    assert type(mccabe["max-complexity"]) is int


def test_pyproject_per_file_ignores_have_no_c901():
    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    ignores = data["tool"]["ruff"]["lint"].get("per-file-ignores", {})
    for pattern, codes in ignores.items():
        assert "C901" not in codes, f"{pattern} must not ignore C901"


def test_src_rebar_has_no_inline_c901_noqa():
    offenders = []
    for py in (REPO_ROOT / "src" / "rebar").rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            low = line.lower()
            if "noqa" in low and "c901" in low:
                offenders.append(f"{py}:{i}")
    assert offenders == [], f"inline C901 noqa found: {offenders}"


def test_committed_baseline_schema_and_count():
    live = _live_count()
    doc = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1
    assert type(doc["schema_version"]) is int
    # Every live finding must have a baseline key, or `--check` would report `new>0`.
    # The baseline may additionally retain keys for functions that have since dropped
    # below the threshold (reported as `stale`, an allowed improvement).
    assert len(doc["ceilings"]) >= live
    # every value > threshold; keys sorted
    keys = list(doc["ceilings"])
    assert keys == sorted(keys)
    for key, ceiling in doc["ceilings"].items():
        assert type(ceiling) is int and ceiling > 15
        ccb._validate_key(key)  # contract-valid key


def test_committed_baseline_loads_and_checks_clean():
    """The committed tree carries no NEW or INCREASED debt.

    `stale` is not asserted away: a legitimate complexity reduction is reported as
    `stale` and still passes the gate. What holds structurally is that the counters
    partition the baseline (they are mutually exclusive) and that only live findings
    sitting exactly at their ceiling can be `active`.
    """
    ceilings = ccb.load_baseline(BASELINE_PATH)
    findings = ccb.run_scanner()
    current = ccb.normalize_findings(findings, ccb.REPO_ROOT)
    counters = ccb.compare(current, ceilings)
    assert counters.new == []
    assert counters.increased == []
    assert len(counters.active) + len(counters.increased) + len(counters.stale) == len(ceilings)
    assert len(counters.active) <= _live_count()


# ───────────────────────────── schema validation ────────────────────────────


def _valid_doc(ceilings: dict[str, int]) -> str:
    return json.dumps({"schema_version": 1, "ceilings": ceilings}, indent=2) + "\n"


VALID_KEY = "src/rebar/example.py::Service.run"


def test_valid_baseline_parses():
    ceilings = ccb.parse_baseline(_valid_doc({VALID_KEY: 20}).encode("utf-8"))
    assert ceilings == {VALID_KEY: 20}


@pytest.mark.parametrize(
    "raw, needle",
    [
        (b"\xff\xfe not utf8", "UTF-8"),
        (b"{ not json", "JSON"),
        (b'{"schema_version": 1}', "exactly"),
        (b'{"ceilings": {}}', "exactly"),
        (b'{"schema_version": 1, "ceilings": {}, "extra": 1}', "exactly"),
        (b'{"schema_version": 2, "ceilings": {}}', "schema_version"),
        (b'{"schema_version": true, "ceilings": {}}', "schema_version"),
        (b'{"schema_version": 1, "ceilings": []}', "object"),
    ],
)
def test_schema_invalid_top_level(raw, needle):
    with pytest.raises(ccb.SchemaError) as exc:
        ccb.parse_baseline(raw)
    assert needle.lower() in str(exc.value).lower()


def test_schema_duplicate_json_member_names():
    raw = b'{"schema_version": 1, "schema_version": 1, "ceilings": {}}'
    with pytest.raises(ccb.SchemaError) as exc:
        ccb.parse_baseline(raw)
    assert "duplicate" in str(exc.value).lower()


def test_schema_duplicate_ceiling_member_names():
    raw = (
        f'{{"schema_version": 1, "ceilings": {{"{VALID_KEY}": 20, "{VALID_KEY}": 21}}}}'
    ).encode()
    with pytest.raises(ccb.SchemaError) as exc:
        ccb.parse_baseline(raw)
    assert "duplicate" in str(exc.value).lower()


def test_schema_unsorted_ceilings():
    raw = b'{"schema_version": 1, "ceilings": {"src/rebar/z.py::z": 20, "src/rebar/a.py::a": 20}}'
    with pytest.raises(ccb.SchemaError) as exc:
        ccb.parse_baseline(raw)
    assert "sorted" in str(exc.value).lower()


@pytest.mark.parametrize(
    "value",
    [15, 10, True, 16.0, "16", None],
)
def test_schema_invalid_ceiling_values(value):
    raw = _valid_doc({VALID_KEY: value}).encode("utf-8")
    with pytest.raises(ccb.SchemaError):
        ccb.parse_baseline(raw)


@pytest.mark.parametrize(
    "key",
    [
        "example.py::run",  # not under src/rebar/
        "src/rebar/example.txt::run",  # not .py
        "src/rebar\\example.py::run",  # backslash
        "/src/rebar/example.py::run",  # absolute
        "src/rebar/../example.py::run",  # .. component
        "src/rebar/example.py::",  # empty symbol
        "src/rebar/example.py::1bad",  # non-identifier segment
        "src/rebar/example.py::a::b",  # two '::'
    ],
)
def test_schema_invalid_keys(key):
    raw = _valid_doc({key: 20}).encode("utf-8")
    with pytest.raises(ccb.SchemaError):
        ccb.parse_baseline(raw)


# ───────────────────────────── symbol identity ──────────────────────────────


SYMBOL_SRC = """
class A:
    def run(self):
        return 1

class B:
    def run(self):
        return 2

def outer():
    def inner():
        return 3
    return inner

async def afunc():
    return 4

square = lambda x: x * x
"""


def _line_of(src: str, needle: str) -> int:
    for i, line in enumerate(src.splitlines(), 1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not found")


def test_symbol_index_ancestry_and_lambda():
    index = ccb.build_symbol_index(SYMBOL_SRC)
    names = {qual for entries in index.values() for _, qual in entries}
    assert names == {"A.run", "B.run", "outer", "outer.inner", "afunc"}
    # lambda never appears
    assert not any("lambda" in n or "square" in n for n in names)


def test_resolve_nested_equal_leaf_under_different_parents():
    a_run = _line_of(SYMBOL_SRC, "class A") + 1
    b_run = _line_of(SYMBOL_SRC, "class B") + 1
    assert ccb.resolve_symbol(SYMBOL_SRC, a_run, 0) == "A.run"
    assert ccb.resolve_symbol(SYMBOL_SRC, b_run, 0) == "B.run"


def test_resolve_async_and_nested():
    assert ccb.resolve_symbol(SYMBOL_SRC, _line_of(SYMBOL_SRC, "async def afunc"), 0) == "afunc"
    assert ccb.resolve_symbol(SYMBOL_SRC, _line_of(SYMBOL_SRC, "def inner"), 0) == "outer.inner"


def test_resolve_lambda_location_has_no_symbol():
    with pytest.raises(ccb.NormalizationError):
        ccb.resolve_symbol(SYMBOL_SRC, _line_of(SYMBOL_SRC, "square = lambda"), 0)


OVERLOAD_SRC = """
from typing import overload

class S:
    @overload
    def m(self, x: int) -> int: ...
    @overload
    def m(self, x: str) -> str: ...
    def m(self, x):
        return x
"""


def test_overload_impl_resolves_to_qualified_name():
    # The concrete implementation is the last `def m` line.
    impl_line = max(
        i for i, line in enumerate(OVERLOAD_SRC.splitlines(), 1) if "def m(self, x)" in line
    )
    assert ccb.resolve_symbol(OVERLOAD_SRC, impl_line, 0) == "S.m"


def _make_src_file(tmp_path: Path, rel: str, source: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _finding(path: Path, row: int, score: int, name: str = "f") -> dict:
    return {
        "code": "C901",
        "filename": str(path),
        "location": {"row": row, "column": 1},
        "message": f"`{name}` is too complex ({score} > 15)",
    }


def test_duplicate_same_qualified_key_fails_normalization(tmp_path):
    src = "def foo():\n    return 1\n\ndef foo():\n    return 2\n"
    path = _make_src_file(tmp_path, "src/rebar/dup.py", src)
    findings = [_finding(path, 1, 20, "foo"), _finding(path, 4, 21, "foo")]
    with pytest.raises(ccb.NormalizationError) as exc:
        ccb.normalize_findings(findings, tmp_path)
    assert "duplicate" in str(exc.value).lower()


def test_score_extraction():
    assert ccb.score_from_message("`f` is too complex (37 > 15)") == 37
    with pytest.raises(ccb.NormalizationError):
        ccb.score_from_message("no score here")


def test_normalize_key_form(tmp_path):
    src = "class Service:\n    def run(self):\n        return 1\n"
    path = _make_src_file(tmp_path, "src/rebar/example.py", src)
    result = ccb.normalize_findings([_finding(path, 2, 20, "run")], tmp_path)
    assert result == {"src/rebar/example.py::Service.run": 20}


# ─────────────────────────────── counters ───────────────────────────────────


def test_counter_new():
    counters = ccb.compare({"src/rebar/a.py::x": 20}, {})
    assert counters.new == ["src/rebar/a.py::x"]
    assert counters.increased == counters.active == counters.stale == []
    assert counters.has_regression


def test_counter_increased():
    counters = ccb.compare({"k": 21}, {"k": 20})
    assert counters.increased == ["k"]
    assert counters.new == counters.active == counters.stale == []
    assert counters.has_regression


def test_counter_active():
    counters = ccb.compare({"k": 20}, {"k": 20})
    assert counters.active == ["k"]
    assert not counters.has_regression


def test_counter_stale_lowered_score():
    counters = ccb.compare({"k": 18}, {"k": 20})
    assert counters.stale == ["k"]
    assert counters.new == counters.increased == counters.active == []
    assert not counters.has_regression


def test_counter_stale_removed_diagnostic():
    counters = ccb.compare({}, {"k": 20})
    assert counters.stale == ["k"]
    assert not counters.has_regression


def test_counter_mutual_exclusivity():
    current = {"a": 20, "b": 25, "c": 18, "d": 20}
    baseline = {"a": 20, "b": 20, "c": 20, "e": 20}
    counters = ccb.compare(current, baseline)
    assert counters.active == ["a"]
    assert counters.increased == ["b"]
    assert counters.stale == ["c", "e"]
    assert counters.new == ["d"]


# ─────────────────────────── scanner outcomes ───────────────────────────────


class _FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _c901_json(name="f", score=20, row=1):
    return json.dumps(
        [
            {
                "code": "C901",
                "filename": "src/rebar/x.py",
                "location": {"row": row, "column": 1},
                "message": f"`{name}` is too complex ({score} > 15)",
            }
        ]
    )


@pytest.mark.parametrize("rc", [0, 1])
def test_scanner_rc0_and_rc1_proceed(monkeypatch, rc):
    monkeypatch.setattr(
        ccb, "subprocess", types.SimpleNamespace(run=lambda *a, **k: _FakeProc(rc, _c901_json()))
    )
    findings = ccb.run_scanner()
    assert len(findings) == 1
    assert findings[0]["code"] == "C901"


def test_scanner_missing_ruff(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("ruff")

    monkeypatch.setattr(ccb, "subprocess", types.SimpleNamespace(run=boom))
    with pytest.raises(ccb.ScannerError):
        ccb.run_scanner()


def test_scanner_rc2_is_failure(monkeypatch):
    monkeypatch.setattr(
        ccb, "subprocess", types.SimpleNamespace(run=lambda *a, **k: _FakeProc(2, "", "boom"))
    )
    with pytest.raises(ccb.ScannerError):
        ccb.run_scanner()


def test_scanner_invalid_json(monkeypatch):
    monkeypatch.setattr(
        ccb, "subprocess", types.SimpleNamespace(run=lambda *a, **k: _FakeProc(0, "not json"))
    )
    with pytest.raises(ccb.ScannerError):
        ccb.run_scanner()


def test_scanner_non_c901_output(monkeypatch):
    payload = json.dumps([{"code": "E501", "filename": "src/rebar/x.py"}])
    monkeypatch.setattr(
        ccb, "subprocess", types.SimpleNamespace(run=lambda *a, **k: _FakeProc(0, payload))
    )
    with pytest.raises(ccb.ScannerError):
        ccb.run_scanner()


def test_scanner_duplicate_current_keys(tmp_path):
    # Two findings resolving to the same qualified key -> normalization failure.
    src = "def foo():\n    return 1\n\ndef foo():\n    return 2\n"
    path = _make_src_file(tmp_path, "src/rebar/dup.py", src)
    findings = [_finding(path, 1, 20, "foo"), _finding(path, 4, 20, "foo")]
    with pytest.raises(ccb.NormalizationError):
        ccb.normalize_findings(findings, tmp_path)


# ─────────────────────────── update-stale / write ───────────────────────────


def test_compute_update_lowers_and_removes():
    current = {"keep": 20, "lower": 18}
    baseline = {"keep": 20, "lower": 22, "gone": 30}
    updated = ccb.compute_update(current, baseline)
    assert updated == {"keep": 20, "lower": 18}


def test_render_baseline_is_canonical_and_sorted():
    text = ccb.render_baseline({"src/rebar/z.py::z": 20, "src/rebar/a.py::a": 21})
    doc = json.loads(text)
    assert list(doc["ceilings"]) == ["src/rebar/a.py::a", "src/rebar/z.py::z"]
    assert text.endswith("\n")
    assert doc["schema_version"] == 1


def test_write_baseline_roundtrips(tmp_path):
    target = tmp_path / "baseline.json"
    ceilings = {"src/rebar/a.py::a": 20, "src/rebar/b.py::b": 30}
    ccb.write_baseline(target, ceilings)
    assert ccb.parse_baseline(target.read_bytes()) == ceilings


def test_write_baseline_byte_idempotent(tmp_path):
    target = tmp_path / "baseline.json"
    ceilings = {"src/rebar/a.py::a": 20}
    ccb.write_baseline(target, ceilings)
    first = target.read_bytes()
    ccb.write_baseline(target, ceilings)
    assert target.read_bytes() == first


def test_write_baseline_injected_replace_failure(tmp_path, monkeypatch):
    import rebar._store.fsutil as fsutil

    target = tmp_path / "baseline.json"
    original = ccb.render_baseline({"src/rebar/a.py::a": 20})
    target.write_text(original, encoding="utf-8")

    def boom(*a, **k):
        raise OSError("injected replace failure")

    monkeypatch.setattr(fsutil.os, "replace", boom)
    with pytest.raises(OSError):
        ccb.write_baseline(target, {"src/rebar/b.py::b": 30})
    # target bytes unchanged
    assert target.read_text(encoding="utf-8") == original
    # no sibling temp file remains
    siblings = [p.name for p in tmp_path.iterdir() if p.name != "baseline.json"]
    assert siblings == []


def _write_committed_style_baseline(path: Path, ceilings: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ccb.render_baseline(ceilings), encoding="utf-8")


def test_update_stale_drains_stale_entries(tmp_path, monkeypatch):
    baseline_file = tmp_path / "baseline.json"
    _write_committed_style_baseline(
        baseline_file,
        {
            "src/rebar/keep.py::keep": 20,
            "src/rebar/lower.py::lower": 22,
            "src/rebar/gone.py::gone": 30,
        },
    )
    monkeypatch.setattr(ccb, "BASELINE_PATH", baseline_file)
    monkeypatch.setattr(ccb, "run_scanner", lambda **k: [])
    monkeypatch.setattr(
        ccb,
        "normalize_findings",
        lambda f, root: {"src/rebar/keep.py::keep": 20, "src/rebar/lower.py::lower": 18},
    )
    rc = ccb._run_update_stale(tmp_path)
    assert rc == 0
    result = ccb.parse_baseline(baseline_file.read_bytes())
    assert result == {"src/rebar/keep.py::keep": 20, "src/rebar/lower.py::lower": 18}


def test_update_stale_byte_idempotent_when_no_stale(tmp_path, monkeypatch):
    baseline_file = tmp_path / "baseline.json"
    _write_committed_style_baseline(baseline_file, {"src/rebar/a.py::a": 20})
    before = baseline_file.read_bytes()
    monkeypatch.setattr(ccb, "BASELINE_PATH", baseline_file)
    monkeypatch.setattr(ccb, "run_scanner", lambda **k: [])
    monkeypatch.setattr(ccb, "normalize_findings", lambda f, root: {"src/rebar/a.py::a": 20})
    rc = ccb._run_update_stale(tmp_path)
    assert rc == 0
    assert baseline_file.read_bytes() == before


@pytest.mark.parametrize(
    "current",
    [
        {"src/rebar/a.py::a": 20, "src/rebar/new.py::new": 20},  # new debt
        {"src/rebar/a.py::a": 25},  # increased debt
    ],
)
def test_update_stale_refuses_regressions(tmp_path, monkeypatch, current):
    baseline_file = tmp_path / "baseline.json"
    _write_committed_style_baseline(baseline_file, {"src/rebar/a.py::a": 20})
    before = baseline_file.read_bytes()
    monkeypatch.setattr(ccb, "BASELINE_PATH", baseline_file)
    monkeypatch.setattr(ccb, "run_scanner", lambda **k: [])
    monkeypatch.setattr(ccb, "normalize_findings", lambda f, root: current)
    rc = ccb._run_update_stale(tmp_path)
    assert rc == 1
    assert baseline_file.read_bytes() == before


def test_update_stale_refuses_on_scanner_error(tmp_path, monkeypatch):
    baseline_file = tmp_path / "baseline.json"
    _write_committed_style_baseline(baseline_file, {"src/rebar/a.py::a": 20})
    before = baseline_file.read_bytes()
    monkeypatch.setattr(ccb, "BASELINE_PATH", baseline_file)

    def boom(**k):
        raise ccb.ScannerError("scanner down")

    monkeypatch.setattr(ccb, "run_scanner", boom)
    rc = ccb._run_update_stale(tmp_path)
    assert rc == 1
    assert baseline_file.read_bytes() == before


def test_update_stale_refuses_on_schema_error(tmp_path, monkeypatch):
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text("{ not valid json", encoding="utf-8")
    before = baseline_file.read_bytes()
    monkeypatch.setattr(ccb, "BASELINE_PATH", baseline_file)
    monkeypatch.setattr(ccb, "run_scanner", lambda **k: [])
    monkeypatch.setattr(ccb, "normalize_findings", lambda f, root: {})
    rc = ccb._run_update_stale(tmp_path)
    assert rc == 1
    assert baseline_file.read_bytes() == before


# ───────────────────────── check orchestration ──────────────────────────────


def test_run_check_reports_regression(tmp_path, monkeypatch, capsys):
    baseline_file = tmp_path / "baseline.json"
    _write_committed_style_baseline(baseline_file, {"src/rebar/a.py::a": 20})
    monkeypatch.setattr(ccb, "BASELINE_PATH", baseline_file)
    monkeypatch.setattr(ccb, "run_scanner", lambda **k: [])
    monkeypatch.setattr(ccb, "normalize_findings", lambda f, root: {"src/rebar/a.py::a": 25})
    rc = ccb._run_check(tmp_path)
    out = capsys.readouterr().out
    assert rc == 1
    assert "increased=1" in out


def test_run_check_allows_stale(tmp_path, monkeypatch, capsys):
    baseline_file = tmp_path / "baseline.json"
    _write_committed_style_baseline(
        baseline_file, {"src/rebar/a.py::a": 20, "src/rebar/b.py::b": 25}
    )
    monkeypatch.setattr(ccb, "BASELINE_PATH", baseline_file)
    monkeypatch.setattr(ccb, "run_scanner", lambda **k: [])
    monkeypatch.setattr(ccb, "normalize_findings", lambda f, root: {"src/rebar/a.py::a": 20})
    rc = ccb._run_check(tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "active=1 new=0 increased=0 stale=1"


# ─────────────────────────────── --help / docs ──────────────────────────────


def test_help_documents_contracts():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    text = proc.stdout
    for token in [
        "--exit-zero",
        "--statistics",
        "--check",
        "--update-stale",
        "schema_version",
        "ceilings",
        "active=",
        "new=",
        "increased=",
        "stale=",
        "Exit-code contract",
    ]:
        assert token in text, f"--help missing {token!r}"


def test_runbook_documents_tokens():
    text = (REPO_ROOT / "docs" / "maintenance-audit-runbook.md").read_text(encoding="utf-8")
    assert "ruff --version" in text
    assert "max-complexity=15" in text
    assert "src/rebar" in text
    assert "python scripts/check_complexity_baseline.py --check" in text


# ────────────────────────────── make lint wiring ────────────────────────────


def test_make_lint_wires_wrapper_and_retains_ruff_format():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    lint_block = makefile.split("\nlint:", 1)[1].split("\n\n", 1)[0]
    assert "python scripts/check_complexity_baseline.py --check" in lint_block
    assert "ruff check $(sources)" in lint_block
    assert "ruff format --check $(sources)" in lint_block


def test_make_sources_cover_src_and_tests():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    # `sources` must span both src and tests (the ruff/format scope).
    for line in makefile.splitlines():
        if line.strip().startswith("sources"):
            assert "src" in line and "tests" in line
            break
    else:
        raise AssertionError("no `sources` variable found in Makefile")


def test_complexity_lock_runs_once_in_the_non_matrix_lint_job() -> None:
    """The commit-level lock must not be multiplied by the OS/Python test matrix."""
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    command = "python scripts/check_complexity_baseline.py --lock"
    invocations = [
        (job_name, step)
        for job_name, job in jobs.items()
        for step in job.get("steps", [])
        for _ in range(str(step.get("run", "")).count(command))
    ]

    assert len(invocations) == 1, "the commit-level complexity lock must execute exactly once"
    job_name, step = invocations[0]
    assert job_name == "lint", "the complexity lock belongs in the single lint job"
    assert "matrix" not in jobs[job_name].get("strategy", {})
    assert not step.get("continue-on-error", False), "the complexity lock must remain blocking"
    assert "-f scripts/check_complexity_baseline.py" in step["run"]


# ── the lock's base of comparison (bug humble-cinnamoned-mussel) ───────────────────────────
# The lock rejects a ceiling HIGHER on the branch than on its base. Judged against main's TIP,
# a branch that never touched the baseline is measured against a main that moved on — so the
# moment any change LOWERS a ceiling on main, every un-rebased branch still carrying the old
# file reads as RAISING it, and the shrink-only ratchet punishes the shrinking it exists to
# reward. It is judged against the MERGE BASE, so only entries the branch itself changed count.

_KEY = "src/rebar/_commands/transition_close.py::close_ticket"


def _baseline_bytes(ceilings: dict) -> str:
    return json.dumps({"schema_version": 1, "ceilings": ceilings}, indent=2, sort_keys=True) + "\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def forked_repo(tmp_path: Path) -> Path:
    """A repo where main LOWERED a ceiling after a branch forked from it.

    This is the exact shape of the incident: change 1673 took close_ticket 19 -> 17 on main,
    and every branch that forked before it still carries 19 in its own tree.
    """
    repo = tmp_path / "repo"
    (repo / ".github").mkdir(parents=True)
    _git(repo.parent, "init", "-q", str(repo))
    for k, v in (("user.email", "t@example.com"), ("user.name", "T")):
        _git(repo, "config", k, v)
    baseline = repo / ".github" / "complexity-baseline.json"

    baseline.write_text(_baseline_bytes({_KEY: 19}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fork point: ceiling 19")
    _git(repo, "branch", "-M", "main")
    fork_point = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-qb", "feature")  # branches here, never touches the baseline
    (repo / "unrelated.txt").write_text("work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated work")

    _git(repo, "checkout", "-q", "main")
    baseline.write_text(_baseline_bytes({_KEY: 17}))  # main LOWERS the ceiling
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "lower the ceiling to 17")

    _git(repo, "checkout", "-q", "feature")
    assert _git(repo, "merge-base", "main", "HEAD") == fork_point
    return repo


def _base_rev(repo: Path, monkeypatch) -> str:
    monkeypatch.setattr(ccb, "REPO_ROOT", repo)
    return ccb._base_commit_for_comparison("main")


def test_the_base_is_the_merge_base_not_mains_tip(forked_repo: Path, monkeypatch):
    """The fix itself: an un-rebased branch is judged against where it forked."""
    rev = _base_rev(forked_repo, monkeypatch)

    assert rev == _git(forked_repo, "merge-base", "main", "HEAD")
    assert rev != _git(forked_repo, "rev-parse", "main")


def test_an_unrebased_branch_passes_after_main_lowers_a_ceiling(forked_repo: Path, monkeypatch):
    """THE REGRESSION. Before the fix this produced, verbatim:

        ceiling RAISED for '...close_ticket': 17 -> 19 — an administrator must override

    on every open change that had not rebased — while the branch had changed nothing.
    """
    rev = _base_rev(forked_repo, monkeypatch)
    base = ccb.parse_baseline(
        subprocess.run(
            ["git", "show", f"{rev}:.github/complexity-baseline.json"],
            cwd=forked_repo,
            capture_output=True,
            check=True,
        ).stdout
    )
    branch = {_KEY: 19}  # what the un-rebased branch still carries

    assert ccb.lock_against_base(base, branch) == []

    tip = ccb.parse_baseline(
        subprocess.run(
            ["git", "show", "main:.github/complexity-baseline.json"],
            cwd=forked_repo,
            capture_output=True,
            check=True,
        ).stdout
    )
    assert ccb.lock_against_base(tip, branch), (
        "sanity: against main's TIP this is exactly the false positive the bug was filed for"
    )


def test_a_genuine_raise_still_fails(forked_repo: Path, monkeypatch):
    """The gate must not be softened: raising a ceiling relative to the fork point is still
    an administrator-only change."""
    rev = _base_rev(forked_repo, monkeypatch)
    base = {_KEY: 19}
    assert rev  # the branch is judged against the fork point, where the ceiling was 19

    violations = ccb.lock_against_base(base, {_KEY: 25})

    assert len(violations) == 1
    assert "RAISED" in violations[0] and _KEY in violations[0]


def test_a_new_entry_still_fails():
    """The other administrator-only change, unaffected by the base being the merge base."""
    violations = ccb.lock_against_base({_KEY: 19}, {_KEY: 19, "src/rebar/x.py::f": 16})

    assert len(violations) == 1
    assert "NEW entry" in violations[0]


def test_a_branch_that_lowers_or_removes_still_passes():
    """Reductions are the behaviour the ratchet exists to reward and must never be flagged."""
    assert ccb.lock_against_base({_KEY: 19}, {_KEY: 16}) == []
    assert ccb.lock_against_base({_KEY: 19}, {}) == []


def test_no_merge_base_falls_back_to_the_tip_and_says_so(tmp_path: Path, monkeypatch, capsys):
    """Unrelated histories cannot produce a merge base. The fallback is main's tip — the
    STRICTER comparison — so an unavailable merge base can never let a genuine raise through."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo.parent, "init", "-q", str(repo))
    for k, v in (("user.email", "t@example.com"), ("user.name", "T")):
        _git(repo, "config", k, v)
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main")
    _git(repo, "branch", "-M", "main")
    _git(repo, "checkout", "-q", "--orphan", "detached")  # no shared history with main
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "orphan")
    monkeypatch.setattr(ccb, "REPO_ROOT", repo)

    rev = ccb._base_commit_for_comparison("main")

    assert rev == "main"
    assert "no merge base" in capsys.readouterr().out


# ──────────────────── local gate vs CI: shrink agreement (bug d319) ──────────


@pytest.mark.parametrize(
    "current, expect_rc, expect_token",
    [
        # A baselined function DROPPED below its ceiling and left the census
        # entirely -- the exact state a behaviour-preserving extraction produces.
        ({"src/rebar/a.py::a": 20}, 0, "stale=1"),
        # A baselined function got simpler but is still over the threshold.
        ({"src/rebar/a.py::a": 20, "src/rebar/b.py::b": 22}, 0, "stale=1"),
    ],
)
def test_tree_assertions_agree_with_gate_on_a_reduction(
    tmp_path, monkeypatch, capsys, current, expect_rc, expect_token
):
    """A genuine complexity REDUCTION must get the SAME verdict locally and in CI.

    ``make lint`` runs ``--check``, which allows ``stale>0`` as an improvement
    (script docstring, "Exit-code contract"). The tree-scoped assertions CI runs
    must not be stricter, or a contributor who improves a baselined function gets a
    green ``make lint`` and a red ``Verified`` -- which is what Gerrit 1928 hit.
    """
    baseline_file = tmp_path / "baseline.json"
    _write_committed_style_baseline(
        baseline_file, {"src/rebar/a.py::a": 20, "src/rebar/b.py::b": 25}
    )
    monkeypatch.setattr(ccb, "BASELINE_PATH", baseline_file)
    monkeypatch.setattr(ccb, "run_scanner", lambda **k: [])
    monkeypatch.setattr(ccb, "normalize_findings", lambda f, root: current)
    # The same synthetic tree as the committed-tree tests see it.
    monkeypatch.setattr(f"{__name__}.BASELINE_PATH", baseline_file)
    monkeypatch.setattr(f"{__name__}._live_census", lambda: ([{"code": "C901"}] * len(current), 1))

    # Half 1 -- the local gate allows the reduction.
    rc = ccb._run_check(tmp_path)
    out = capsys.readouterr().out
    assert rc == expect_rc, out
    assert expect_token in out

    # Half 2 -- the assertions CI enforces on that same tree must agree.
    test_committed_baseline_schema_and_count()
    test_committed_baseline_loads_and_checks_clean()


@pytest.mark.parametrize(
    "current, expect_token",
    [
        ({"src/rebar/a.py::a": 20, "src/rebar/b.py::b": 25, "src/rebar/c.py::c": 30}, "new=1"),
        ({"src/rebar/a.py::a": 33, "src/rebar/b.py::b": 25}, "increased=1"),
    ],
)
def test_shrink_only_guard_still_bites(tmp_path, monkeypatch, capsys, current, expect_token):
    """Tolerating ``stale`` must not weaken the ratchet: new/increased still fail."""
    baseline_file = tmp_path / "baseline.json"
    _write_committed_style_baseline(
        baseline_file, {"src/rebar/a.py::a": 20, "src/rebar/b.py::b": 25}
    )
    monkeypatch.setattr(ccb, "BASELINE_PATH", baseline_file)
    monkeypatch.setattr(ccb, "run_scanner", lambda **k: [])
    monkeypatch.setattr(ccb, "normalize_findings", lambda f, root: current)

    rc = ccb._run_check(tmp_path)
    out = capsys.readouterr().out
    assert rc == 1, out
    assert expect_token in out
