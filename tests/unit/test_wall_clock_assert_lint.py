"""Wall-clock upper-bound assert lint (ticket 1e95-fc5c-bca8-44c7).

DET code-review gate over ``tests/**``: an upper-bound wall-clock assert
(``assert elapsed < N`` and kin) is the proven CI flake class under runner
contention (bugs 19d7, 5e94, edfe, 85c3). Two escapes:

- ``# timing: hang-guard — <reason>`` on the assert (reason mandatory), or
- the perf-lane CI-exclusion guard ``@pytest.mark.skipif(os.environ.get("CI")
  == "true", ...)`` on the enclosing test (the bare ``@pytest.mark.benchmark``
  marker is NOT an escape — no CI invocation filters it out).

These tests drive the lint against synthetic trees under tmp_path; the
fixtures deliberately contain live upper-bound asserts, so this module is the
lint's EXCLUDED_FILES fixture corpus (the check_comment_hygiene.py idiom).
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_wall_clock_asserts.py"


@pytest.fixture(scope="module")
def lint():
    spec = importlib.util.spec_from_file_location("check_wall_clock_asserts", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_wall_clock_asserts"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    return tmp_path


def _t(tree: Path, rel: str, body: str) -> Path:
    p = tree / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _findings(lint, tree: Path):
    return lint.scan_tree(tree)


# ── fire: unescaped upper-bound wall-clock asserts ──────────────────────────


def test_unescaped_elapsed_assert_fires(lint, tree: Path) -> None:
    _t(
        tree,
        "tests/unit/test_x.py",
        """
        import time

        def test_speed():
            start = time.perf_counter()
            do_work()
            elapsed = time.perf_counter() - start
            assert elapsed < 5.0
        """,
    )
    fs = _findings(lint, tree)
    assert len(fs) == 1
    assert fs[0].path.name == "test_x.py"
    assert "elapsed < 5.0" in fs[0].text


def test_inline_monotonic_subtraction_fires(lint, tree: Path) -> None:
    _t(
        tree,
        "tests/unit/test_y.py",
        """
        import time as _time

        def test_immediate():
            t0 = _time.monotonic()
            poke()
            assert _time.monotonic() - t0 < 1.0, "must surface immediately"
        """,
    )
    assert len(_findings(lint, tree)) == 1


def test_elapsed_ms_and_le_fires(lint, tree: Path) -> None:
    _t(
        tree,
        "tests/unit/test_z.py",
        """
        def test_budget():
            elapsed_ms = measure()
            assert elapsed_ms <= 500
        """,
    )
    assert len(_findings(lint, tree)) == 1


def test_multiline_assert_fires(lint, tree: Path) -> None:
    _t(
        tree,
        "tests/unit/test_m.py",
        """
        def test_retry():
            elapsed = run()
            assert elapsed < 20.0, (
                "retries must not stack timeouts"
            )
        """,
    )
    assert len(_findings(lint, tree)) == 1


# ── silent: escapes and out-of-scope shapes ─────────────────────────────────


def test_hang_guard_marker_escapes(lint, tree: Path) -> None:
    _t(
        tree,
        "tests/unit/test_g.py",
        """
        def test_no_stall():
            elapsed = run()
            assert elapsed < 8.0  # timing: hang-guard — stall detector, 8s >> ms-scale op
        """,
    )
    assert _findings(lint, tree) == []


def test_marker_without_reason_still_fires(lint, tree: Path) -> None:
    _t(
        tree,
        "tests/unit/test_bare.py",
        """
        def test_no_stall():
            elapsed = run()
            assert elapsed < 8.0  # timing: hang-guard
        """,
    )
    fs = _findings(lint, tree)
    assert len(fs) == 1
    assert "reason" in fs[0].why


def test_skipif_ci_guard_escapes(lint, tree: Path) -> None:
    _t(
        tree,
        "tests/unit/test_perf.py",
        """
        import os
        import pytest

        @pytest.mark.benchmark
        @pytest.mark.skipif(
            os.environ.get("CI") == "true",
            reason="wall-clock benchmark skipped on CI runners",
        )
        def test_bench():
            elapsed = run()
            assert elapsed < 0.5
        """,
    )
    assert _findings(lint, tree) == []


def test_bare_benchmark_marker_does_not_escape(lint, tree: Path) -> None:
    _t(
        tree,
        "tests/unit/test_bench.py",
        """
        import pytest

        @pytest.mark.benchmark
        def test_bench():
            elapsed = run()
            assert elapsed < 0.5
        """,
    )
    assert len(_findings(lint, tree)) == 1


def test_lower_bound_assert_is_silent(lint, tree: Path) -> None:
    _t(
        tree,
        "tests/unit/test_lb.py",
        """
        def test_debounce_waited():
            elapsed = run()
            assert elapsed > 0.2
            assert elapsed >= 0.2
        """,
    )
    assert _findings(lint, tree) == []


def test_non_timing_comparison_is_silent(lint, tree: Path) -> None:
    _t(
        tree,
        "tests/unit/test_nt.py",
        """
        def test_count():
            items = build()
            assert len(items) < 100
        """,
    )
    assert _findings(lint, tree) == []


def test_excluded_files_skips_own_fixture_corpus(lint, tree: Path) -> None:
    _t(
        tree,
        "tests/unit/test_wall_clock_assert_lint.py",
        """
        def test_fixture():
            elapsed = run()
            assert elapsed < 5.0
        """,
    )
    assert _findings(lint, tree) == []


# ── CLI contract ────────────────────────────────────────────────────────────


def test_main_exit_and_teaching_message(lint, tree: Path, capsys) -> None:
    _t(
        tree,
        "tests/unit/test_x.py",
        """
        def test_speed():
            elapsed = run()
            assert elapsed < 5.0
        """,
    )
    rc = lint.main([str(tree)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "timing: hang-guard" in out
    assert "skipif" in out


def test_main_clean_tree_exit_zero(lint, tree: Path, capsys) -> None:
    _t(
        tree,
        "tests/unit/test_ok.py",
        """
        def test_logic():
            assert compute() == 4
        """,
    )
    assert lint.main([str(tree)]) == 0
