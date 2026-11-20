"""Text I/O names UTF-8 at every boundary (bug obtuse-phenocryst-verdin).

On Windows, text I/O without ``encoding=`` uses the ANSI code page (cp1252) and text
writes turn ``\\n`` into ``\\r\\n``. These tests reproduce each Windows failure on any host:
by running a child under ``-X warn_default_encoding`` with ``EncodingWarning`` as an error,
by forcing a cp1252 stdio codec, or by routing a write through the pure-Python I/O stack
with a patched ``os.linesep``.
"""

from __future__ import annotations

import _pyio
import ast
import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from _repo_root import REPO_ROOT
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

_HOOK_DRIVER = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    import hatch_build

    hook = hatch_build.CustomBuildHook(sys.argv[2], {}, None, None, sys.argv[2], "wheel")
    hook.initialize("0", {})
    """
)


def _run_hook(tree: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-X",
            "warn_default_encoding",
            "-W",
            "error::EncodingWarning",
            "-c",
            _HOOK_DRIVER,
            str(REPO_ROOT),
            str(tree),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=subprocess_env(env),
        check=False,
    )


def test_the_build_hook_writes_the_generated_module_as_utf8(tmp_path: Path) -> None:
    (tmp_path / "src" / "rebar").mkdir(parents=True)
    done = _run_hook(tmp_path, REBAR_BUILD_COMMIT="abc1234")
    assert done.returncode == 0, done.stderr
    text = (tmp_path / "src" / "rebar" / "_build_info.py").read_bytes().decode("utf-8")
    assert "COMMIT = 'abc1234'" in text


def test_the_build_hook_degrades_a_stale_cp1252_module_to_no_baked_sha(tmp_path: Path) -> None:
    target = tmp_path / "src" / "rebar" / "_build_info.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b'"""Generated \x97 stale."""\n\nCOMMIT = \'deadbee\'\n')
    sys.path.insert(0, str(REPO_ROOT))
    try:
        import hatch_build
    finally:
        sys.path.remove(str(REPO_ROOT))
    assert hatch_build._read_existing_commit(target) is None


def test_rebar_explain_writes_utf8_through_a_cp1252_stdout() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "rebar", "explain", "plan"],
        capture_output=True,
        env=subprocess_env(PYTHONIOENCODING="cp1252"),
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert "plan-review gate" in done.stdout.decode("utf-8")


def _cp1252_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


def test_cli_stdio_is_switched_to_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar import _cli

    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(sys, name, _cp1252_stream())
    _cli._use_utf8_stdio()
    assert [s.encoding for s in (sys.stdin, sys.stdout, sys.stderr)] == ["utf-8"] * 3


def test_a_non_wrapper_stream_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar import _cli

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    _cli._use_utf8_stdio()
    assert sys.stdout is captured


_SUBPROCESS_RUNNERS = frozenset({"run", "Popen", "check_output", "check_call", "call"})


def _is_subprocess_runner(func: ast.expr) -> bool:
    if not (isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_RUNNERS):
        return False
    owner = func.value
    return (isinstance(owner, ast.Name) and owner.id == "subprocess") or (
        isinstance(owner, ast.Attribute) and owner.attr == "subprocess"
    )


def _locale_decoded_text_calls(source: str) -> list[int]:
    """Lines of calls that run a child in text mode without naming an encoding.

    ``encoding=None`` counts as unnamed: it selects the locale codec too.

    Such a call decodes the child's output (and encodes its stdin) with the locale codec,
    which is cp1252 on Windows. A call counts when it passes ``text`` or
    ``universal_newlines`` (not literally false) and no ``encoding``, and either calls a
    ``subprocess`` runner or passes the literal ``True``; the second arm catches keyword
    bundles built with ``dict(...)`` and injected runners.
    """
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        mode = kwargs.get("text", kwargs.get("universal_newlines"))
        if mode is None:
            continue
        encoding = kwargs.get("encoding")
        if encoding is not None and not (
            isinstance(encoding, ast.Constant) and encoding.value is None
        ):
            continue
        if isinstance(mode, ast.Constant) and mode.value in (False, None):
            continue
        if _is_subprocess_runner(node.func) or (
            isinstance(mode, ast.Constant) and mode.value is True
        ):
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ('subprocess.run(["git"], text=True)', True),
        ('subprocess.run(["git"], text=True, encoding="utf-8")', False),
        ('subprocess.run(["git"], text=True, encoding=None)', True),
        ('mod.subprocess.check_output(["git"], universal_newlines=flag)', True),
        ('subprocess.Popen(["git"], text=False)', False),
        ("dict(stdout=PIPE, text=True)", True),
        ("find_similar(text=query)", False),
    ],
)
def test_the_scan_flags_text_mode_calls_without_an_encoding(source: str, flagged: bool) -> None:
    assert bool(_locale_decoded_text_calls(source)) is flagged


def test_no_src_call_decodes_child_output_with_the_locale_codec() -> None:
    """Every text-mode child in src/rebar names UTF-8 (bug dogtired-blank-fieldmouse)."""
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{line}"
        for path in sorted((REPO_ROOT / "src" / "rebar").rglob("*.py"))
        for line in _locale_decoded_text_calls(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_binding_files_keep_lf_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.reconciler import binding_repository

    monkeypatch.setattr(os, "linesep", "\r\n")
    monkeypatch.setattr(binding_repository.os, "fdopen", _pyio.open)
    target = tmp_path / "bindings.json"
    binding_repository._write_json_atomically(target, {"b": 1, "a": 2}, prefix="b")
    raw = target.read_bytes()
    assert b"\r" not in raw
    assert json.loads(raw) == {"a": 2, "b": 1}
