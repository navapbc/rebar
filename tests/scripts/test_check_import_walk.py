"""Check standalone script and installed-package imports deterministically.

Each script imports in isolation so a sibling's ``sys.path`` insert cannot
mask a missing local import. The package walk reports every broken module;
an expected optional module is skipped only for its declared missing dependency.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import check_import_walk as walk
import pytest

pytestmark = pytest.mark.scripts


def _write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body))


@pytest.fixture
def broken_scripts_dir(tmp_path: Path) -> Path:
    """The exact pre-fix shape from spinal-grayish-perch: a bare sibling import, no insert."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    _write(scripts / "dedup.py", "MARKER = 'dedup'\n")
    _write(scripts / "bridge.py", "import dedup\n\nVALUE = dedup.MARKER\n")
    return scripts


class TestScriptsLeg:
    def test_catches_the_recreated_alert_dedup_shape(self, broken_scripts_dir: Path) -> None:
        failures, skips, _attempted = walk.walk_scripts(broken_scripts_dir, skips={})
        failed = {f.name for f in failures}
        assert "bridge.py" in failed, "the pre-fix bare-sibling-import shape must be caught"
        assert not skips
        (failure,) = [f for f in failures if f.name == "bridge.py"]
        assert "No module named 'dedup'" in failure.error

    def test_passes_with_the_documented_insert(self, broken_scripts_dir: Path) -> None:
        _write(
            broken_scripts_dir / "bridge.py",
            """
            import os
            import sys

            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import dedup

            VALUE = dedup.MARKER
            """,
        )
        failures, _skips, _attempted = walk.walk_scripts(broken_scripts_dir, skips={})
        assert failures == []

    def test_sibling_insert_cannot_mask_the_missing_one(self, broken_scripts_dir: Path) -> None:
        """Do not let an earlier script's path insert mask a later bare import.

        ``aaa_inserter.py`` sorts first; process isolation must keep its path and
        module cache from making ``bridge.py`` pass.
        """
        _write(
            broken_scripts_dir / "aaa_inserter.py",
            """
            import os
            import sys

            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import dedup

            VALUE = dedup.MARKER
            """,
        )
        failures, _skips, _attempted = walk.walk_scripts(broken_scripts_dir, skips={})
        assert {f.name for f in failures} == {"bridge.py"}

    def test_explicit_skip_is_honored_and_reported(self, broken_scripts_dir: Path) -> None:
        failures, skips, _attempted = walk.walk_scripts(
            broken_scripts_dir, skips={"bridge.py": "recreated defect, skipped for this test"}
        )
        assert failures == []
        assert [s.name for s in skips] == ["bridge.py"]


@pytest.fixture
def broken_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A synthetic installed package with TWO independently broken modules."""
    pkg = tmp_path / "walkprobe_pkg"
    pkg.mkdir()
    _write(pkg / "__init__.py", "")
    _write(pkg / "good.py", "VALUE = 1\n")
    _write(pkg / "broken_a.py", "import walkprobe_missing_dep_a\n")
    sub = pkg / "sub"
    sub.mkdir()
    _write(sub / "__init__.py", "")
    _write(sub / "broken_b.py", "import walkprobe_missing_dep_b\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    return "walkprobe_pkg"


class TestInstalledPackageLeg:
    def test_reports_all_failures_not_fail_fast(self, broken_package: str) -> None:
        failures, skips, _attempted = walk.walk_package(broken_package, expected_optional={})
        assert {f.name for f in failures} == {
            f"{broken_package}.broken_a",
            f"{broken_package}.sub.broken_b",
        }
        assert not skips

    def test_expected_optional_missing_dep_is_a_skip(self, broken_package: str) -> None:
        expected = {
            f"{broken_package}.broken_a": walk.ExpectedOptional(
                dep="walkprobe_missing_dep_a", extra="agents", reason="test entry"
            ),
        }
        failures, skips, _attempted = walk.walk_package(broken_package, expected_optional=expected)
        assert {f.name for f in failures} == {f"{broken_package}.sub.broken_b"}
        assert [s.name for s in skips] == [f"{broken_package}.broken_a"]

    def test_expected_optional_different_missing_name_is_a_failure(
        self, broken_package: str
    ) -> None:
        expected = {
            f"{broken_package}.broken_a": walk.ExpectedOptional(
                dep="some_other_dep", extra="agents", reason="wrong dep declared"
            ),
        }
        failures, _skips, _attempted = walk.walk_package(broken_package, expected_optional=expected)
        assert f"{broken_package}.broken_a" in {f.name for f in failures}


class TestMain:
    def test_exit_nonzero_and_full_report_on_failures(
        self, broken_scripts_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = walk.main(["--leg", "scripts", "--scripts-dir", str(broken_scripts_dir)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "bridge.py" in out
        assert "No module named 'dedup'" in out

    def test_exit_zero_on_clean_scripts_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        _write(scripts / "fine.py", "VALUE = 1\n")
        rc = walk.main(["--leg", "scripts", "--scripts-dir", str(scripts)])
        assert rc == 0
        assert "0 failed" in capsys.readouterr().out
