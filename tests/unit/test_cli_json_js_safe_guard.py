"""Self-tests for the CLI ``--output json`` JS-safe-integer gate (bug e127-a3ad-895a-4a2f).

The gate flags a stdout write built by a RAW ``json.dumps`` on the CLI surface — the construct
that put rebar's 19-digit nanosecond timestamps on the wire as bare JSON numbers, which float64
consumers silently round and BigInt consumers cannot re-serialize. These tests pin the
DISCRIMINATION (the flagged stdout shapes fail; the ``js_safe_dumps`` choke point, a non-stdout
``json.dumps``, and prose do not), the ``# js-safe-ok: <reason>`` sanction, the reasonless-marker
diagnostic, the loud handling of an unparseable source, that the real tree is clean, and the
gate's own wiring into ``make lint``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_cli_json_js_safe.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import check_cli_json_js_safe as gate  # noqa: E402


def _scan(tmp_path: Path, source: str) -> tuple[list, list]:
    """Run the gate over a synthetic file placed in a scanned CLI root."""
    src = tmp_path / "src" / "rebar" / "_cli"
    src.mkdir(parents=True)
    (src / "sample.py").write_text(source, encoding="utf-8")
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


# ─────────────────────────── the real tree, and wiring ───────────────────────────


def test_the_repository_is_clean() -> None:
    """The whole CLI surface routes through js_safe_dumps (or is sanctioned)."""
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT)], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_make_lint_invokes_the_gate() -> None:
    """A CI-only gate lets a local verdict be green over a tree CI rejects."""
    text = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    body: list[str] = []
    in_target = False
    for line in text.splitlines():
        if re.match(r"^lint:", line):
            in_target = True
            continue
        if in_target and re.match(r"^[A-Za-z0-9_.-]+:", line):
            break
        if in_target:
            body.append(line)
    assert "scripts/check_cli_json_js_safe.py" in "\n".join(body), (
        "`make lint` does not invoke the CLI js-safe-integer gate"
    )
