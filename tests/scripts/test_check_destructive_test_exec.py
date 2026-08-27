"""Tests for the destructive test-exec gate [rebar:6818-615f-555e-4bb9].

The gate exists because on 2026-08-26 a test ``subprocess``-exec'd a real script
containing ``rm -rf "${dir}"/*`` with ``dir`` set to ``""``; the glob expanded to
``rm -rf /*``. These tests pin the discrimination that matters: the unguarded shape
must fail, and BOTH sanctioned remediations — an injectable seam and a ``${var:?}``
guard — must pass, so the gate never punishes the fix it recommends.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CHK_PATH = REPO_ROOT / "scripts" / "check_destructive_test_exec.py"

VULNERABLE = '#!/bin/sh\ndir="$REBAR_TRACKER_DIR"\nrm -rf "${dir}"/.[!.]* "${dir}"/*\n'
SEAMED = '#!/bin/sh\ndir="$REBAR_TRACKER_DIR"\n"${RM_CMD:-rm}" -rf "${dir}"/*\n'
GUARDED = '#!/bin/sh\ndir="$REBAR_TRACKER_DIR"\n: "${dir:?empty}"\nrm -rf "${dir}"/*\n'
RELATIVE = '#!/bin/sh\ncd -- "$1" || exit 1\nrm -rf -- ./*\n'
SET_U_ONLY = '#!/bin/sh\nset -euo pipefail\ndir="$REBAR_TRACKER_DIR"\nrm -rf "${dir}"/*\n'

TEST_EXEC = (
    "import subprocess\n"
    "def test_it(tmp_path):\n"
    "    subprocess.run(['sh', 'purge.sh'], check=False)\n"
)


def _load():
    spec = importlib.util.spec_from_file_location("check_destructive_test_exec", CHK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves cls.__module__ through sys.modules,
    # which is empty for a spec_from_file_location module until it is registered.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


chk = _load()


def _repo(tmp_path: Path, script: str, test_src: str = TEST_EXEC) -> Path:
    (tmp_path / "purge.sh").write_text(script, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_thing.py").write_text(test_src, encoding="utf-8")
    return tmp_path


def test_unguarded_exec_is_flagged(tmp_path):
    repo = _repo(tmp_path, VULNERABLE)
    findings = chk.find_violations(repo)
    assert len(findings) >= 1
    assert findings[0].kind == "destructive-test-exec"
    assert chk.main(["--root", str(repo)]) == 1


def test_finding_reports_the_test_path_and_line(tmp_path):
    repo = _repo(tmp_path, VULNERABLE)
    f = chk.find_violations(repo)[0]
    assert f.path == "tests/test_thing.py"
    assert f.line == 3  # the subprocess.run call site


def test_seam_shape_passes(tmp_path):
    # "${RM_CMD:-rm}" is the remediation the gate recommends — it must not be flagged.
    repo = _repo(tmp_path, SEAMED)
    assert chk.find_violations(repo) == []
    assert chk.main(["--root", str(repo)]) == 0


def test_var_abort_guard_shape_passes(tmp_path):
    repo = _repo(tmp_path, GUARDED)
    assert chk.find_violations(repo) == []
    assert chk.main(["--root", str(repo)]) == 0


def test_relative_glob_after_cd_passes(tmp_path):
    # `rm -rf -- ./*` carries no interpolation, so there is nothing to expand to "/".
    repo = _repo(tmp_path, RELATIVE)
    assert chk.find_violations(repo) == []


def test_set_u_alone_does_not_clear_the_gate(tmp_path):
    # `set -u` fires on UNSET, not set-but-empty — and the incident's var was "".
    repo = _repo(tmp_path, SET_U_ONLY)
    assert len(chk.find_violations(repo)) >= 1


def test_commented_out_deletion_is_ignored(tmp_path):
    repo = _repo(tmp_path, '#!/bin/sh\n# rm -rf "${dir}"/*\necho ok\n')
    assert chk.find_violations(repo) == []


def test_non_exec_subprocess_free_test_is_ignored(tmp_path):
    repo = _repo(tmp_path, VULNERABLE, test_src="def test_it():\n    assert True\n")
    assert chk.find_violations(repo) == []


def test_missing_script_is_not_flagged(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir(parents=True)
    (tests / "test_thing.py").write_text(TEST_EXEC, encoding="utf-8")
    assert chk.find_violations(tmp_path) == []


def test_repo_with_no_tests_dir_passes(tmp_path):
    assert chk.find_violations(tmp_path) == []
    assert chk.main(["--root", str(tmp_path)]) == 0


def test_repository_tree_is_currently_clean():
    assert chk.main(["--root", str(REPO_ROOT)]) == 0
