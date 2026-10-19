"""CLI JSON output must quote integers beyond JavaScript's safe range, including nanosecond
timestamps. This source guard catches raw json.dumps writes that would silently round values in
float64 clients."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import check_cli_json_js_safe as gate  # noqa: E402


def _scan(tmp_path: Path, source: str) -> tuple[list, list]:
    """Run the gate over a synthetic file placed in a scanned CLI root."""
    src = tmp_path / "src" / "rebar" / "_cli"
    src.mkdir(parents=True)
    (src / "sample.py").write_text(source, encoding="utf-8")
    return gate.find_violations(tmp_path)


def _scan_at(tmp_path: Path, rel_dir: str, source: str) -> tuple[list, list]:
    """Run the gate with a synthetic file placed at an arbitrary path under the repo root."""
    target = tmp_path / rel_dir
    target.mkdir(parents=True)
    (target / "sample.py").write_text(source, encoding="utf-8")
    return gate.find_violations(tmp_path)


# ─────────────────────────── the construct is rejected ───────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import json\nprint(json.dumps(doc))\n", id="print-direct"),
        pytest.param(
            "import sys, json\nsys.stdout.write(json.dumps(doc) + '\\n')\n", id="stdout-write-plus"
        ),
        pytest.param(
            "import json\nfrom sys import stdout\nstdout.write(json.dumps(doc) + '\\n')\n",
            id="bare-stdout-write",
        ),
        pytest.param("import json as _json\nprint(_json.dumps(doc))\n", id="underscore-json-alias"),
        pytest.param(
            "import sys, json\nsys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False))\n",
            id="with-kwargs",
        ),
        pytest.param(
            "import sys, json\nsys.stdout.write(f'x: {json.dumps(doc)}\\n')\n",
            id="f-string-embed",
        ),
        pytest.param(
            "import sys, json\n"
            "sys.stdout.write(\n    json.dumps(\n        doc,\n    )\n    + '\\n'\n)\n",
            id="multiline",
        ),
        pytest.param(
            "import sys, json\nprint(json.dumps(doc), file=sys.stdout)\n",
            id="print-file-explicit-stdout",
        ),
        pytest.param(
            "import json\nout = pick_stream()\nprint(json.dumps(doc), file=out)\n",
            id="print-file-ambiguous-variable-fails-closed",
        ),
    ],
)
def test_each_stdout_dumps_shape_is_rejected(tmp_path: Path, source: str) -> None:
    violations, bare = _scan(tmp_path, source)
    assert len(violations) == 1 and bare == [], (
        f"expected one violation, got {[v.text for v in violations]} / bare {bare}"
    )


# ───────────────────────── legitimate uses are NOT rejected ─────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "from rebar._mcp_errors import js_safe_dumps\nprint(js_safe_dumps(doc))\n",
            id="choke-point",
        ),
        pytest.param(
            "import json\nfh.write(json.dumps(record, sort_keys=True) + '\\n')\n",
            id="file-write-not-stdout",
        ),
        pytest.param(
            "import json\ndef canon(v):\n    return json.dumps(v, sort_keys=True)\n",
            id="return-helper-not-stdout",
        ),
        pytest.param(
            "import json\nbody = json.dumps({'k': 'v'}).encode()\n", id="http-body-not-stdout"
        ),
        pytest.param('"""print(json.dumps(x)) in a docstring."""\n', id="docstring"),
        pytest.param("x = 1  # never print(json.dumps(x))\n", id="comment"),
        pytest.param(
            "import sys, json\nprint(json.dumps(diag), file=sys.stderr)\n",
            id="print-to-stderr-allowed",
        ),
        pytest.param(
            "import json\nfrom sys import stderr\nprint(json.dumps(diag), file=stderr)\n",
            id="print-to-bare-stderr-allowed",
        ),
    ],
)
def test_legitimate_and_prose_are_not_flagged(tmp_path: Path, source: str) -> None:
    violations, bare = _scan(tmp_path, source)
    assert violations == [] and bare == [], (
        f"must not be flagged: {[v.text for v in violations + bare]}"
    )


# ────────────────────────────── the sanction ──────────────────────────────


def test_a_reasoned_marker_sanctions_the_line(tmp_path: Path) -> None:
    violations, bare = _scan(
        tmp_path,
        "import json\nprint(json.dumps(names))  # js-safe-ok: list of backend names, no ns ts\n",
    )
    assert violations == [] and bare == []


def test_a_marker_on_the_line_above_sanctions_it(tmp_path: Path) -> None:
    violations, bare = _scan(
        tmp_path,
        "import json\n# js-safe-ok: fixed diagnostic string, no store timestamps\n"
        "print(json.dumps(names))\n",
    )
    assert violations == [] and bare == []


def test_a_marker_on_the_statement_first_line_sanctions_it(tmp_path: Path) -> None:
    """A multi-line stdout write carries its marker on the statement's first line.

    The ``json.dumps`` sits two lines below the marker, so neither the offending line nor the
    line-above candidate covers it — only the ``stmt_line`` candidate in ``_marked`` does.
    """
    violations, bare = _scan(
        tmp_path,
        "import sys, json\n"
        "sys.stdout.write(  # js-safe-ok: fixed banner, no store timestamps\n"
        '    ""\n'
        "    + json.dumps(names)\n"
        ")\n",
    )
    assert violations == [] and bare == []


def test_a_reasonless_marker_is_reported_as_reasonless_not_as_unmarked(tmp_path: Path) -> None:
    violations, bare = _scan(tmp_path, "import json\nprint(json.dumps(doc))  # js-safe-ok\n")
    assert violations == []
    assert len(bare) == 1


def test_an_empty_reason_does_not_sanction(tmp_path: Path) -> None:
    violations, bare = _scan(tmp_path, "import json\nprint(json.dumps(doc))  # js-safe-ok:   \n")
    assert len(violations) + len(bare) == 1


# ─────────────────────── an unparseable source is loud, not silent ───────────────────────


def test_unparseable_source_is_reported_not_skipped(tmp_path: Path) -> None:
    violations, _ = _scan(tmp_path, "import json\nprint(json.dumps(doc))\ndef (:\n")
    assert len(violations) == 1
    assert violations[0].text.startswith("unparseable source")


# ─────────────────────────── scoping is bounded to the CLI surface ───────────────────────────


@pytest.mark.parametrize(
    "rel_dir",
    [
        pytest.param("src/rebar/_logging", id="structured-logging-stream"),
        pytest.param("src/rebar/reconciler", id="reconciler-daemon"),
        pytest.param("src/rebar/_other", id="unrelated-module"),
    ],
)
def test_a_dumps_outside_the_scan_roots_is_not_flagged(tmp_path: Path, rel_dir: str) -> None:
    """The gate pins the CLI ``--output json`` wire, not every ``json.dumps`` in the tree."""
    violations, bare = _scan_at(tmp_path, rel_dir, "import json\nprint(json.dumps(doc))\n")
    assert violations == [] and bare == []


# ─────────────────────────── the exit-code contract ───────────────────────────


def test_main_returns_zero_on_a_clean_root(tmp_path: Path) -> None:
    (tmp_path / "src" / "rebar" / "_cli").mkdir(parents=True)
    assert gate.main(["--root", str(tmp_path)]) == 0


def test_main_returns_nonzero_when_a_violation_exists(tmp_path: Path) -> None:
    src = tmp_path / "src" / "rebar" / "_cli"
    src.mkdir(parents=True)
    (src / "sample.py").write_text("import json\nprint(json.dumps(doc))\n", encoding="utf-8")
    assert gate.main(["--root", str(tmp_path)]) == 1


# ─────────────────────────── the real tree, and wiring ───────────────────────────
