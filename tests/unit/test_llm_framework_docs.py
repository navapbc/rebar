"""The client-facing eval-harness docs stay true to the code (epic e47b slice 3).

Written to derive from the DATA rather than from a hand-kept list. The sibling corpus-move
story needed six plan-review rounds, and nearly every blocking finding was a hand-enumerated
reference that had drifted or been missed; a doc test that restates the same list by hand
inherits exactly that failure mode. So the extras assertion iterates
:data:`rebar._optional.EXTRAS` — a future extra fails the DOC, not this test — and the
command assertions shell out to each documented command's ``--help`` rather than comparing
command strings to a copy of themselves.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from rebar._optional import EXTRAS

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
_FRAMEWORK = REPO_ROOT / "docs" / "llm-framework.md"
_EXAMPLES = REPO_ROOT / "docs" / "llm-example-configs.md"
_RELEASE_NOTES = REPO_ROOT / "docs" / "release-notes.md"

_SECTION = "## Running the eval harnesses against your own criteria"


def _section_body() -> str:
    text = _FRAMEWORK.read_text(encoding="utf-8")
    start = text.index(_SECTION)
    nxt = text.find("\n## ", start + len(_SECTION))
    return text[start : nxt if nxt != -1 else len(text)]


def test_the_client_harness_section_exists() -> None:
    assert _SECTION in _FRAMEWORK.read_text(encoding="utf-8")


def test_every_registered_extra_appears_in_the_taxonomy() -> None:
    """Asserted against ``EXTRAS`` itself, so registering an extra without documenting it
    fails here. A literal count cannot do that — the prose said "three extras" while the
    code held nine."""
    taxonomy = _FRAMEWORK.read_text(encoding="utf-8")
    missing = [name for name in EXTRAS if f"`[{name}]`" not in taxonomy]
    assert missing == [], f"extras registered but undocumented: {missing}"


def test_the_taxonomy_count_matches_the_registry() -> None:
    words = {
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
    }
    expected = words[len(EXTRAS)]
    assert f"The optional surface is {expected} extras" in _FRAMEWORK.read_text(encoding="utf-8")


def _documented_commands() -> list[list[str]]:
    """The ``rebar`` and ``python -m`` invocations in the section's fenced blocks."""
    blocks = re.findall(r"```bash\n(.*?)```", _section_body(), re.S)
    out: list[list[str]] = []
    for block in blocks:
        for line in block.splitlines():
            line = line.strip()
            if line.startswith(("rebar ", "python -m ")):
                out.append(line.split())
    return out


def test_the_section_documents_commands() -> None:
    assert len(_documented_commands()) >= 7


@pytest.mark.parametrize("argv", _documented_commands(), ids=lambda a: " ".join(a[:4]))
def test_every_documented_command_resolves(argv: list[str], tmp_path: Path) -> None:
    """``--help`` on each documented invocation, so a renamed subcommand or a dropped
    module fails here instead of silently misleading a client.

    Run from ``tmp_path``, not the checkout: ``--help`` needs no store, and the unit tier
    requires a harness-owned root so a child cannot resolve ``REBAR_ROOT`` to this tree.
    """
    if argv[0] == "rebar" and shutil.which("rebar") is None:
        pytest.skip("rebar console script not installed")
    cmd = [sys.executable, *argv[1:]] if argv[0] == "python" else list(argv)
    # drop placeholder operands; --help does not need them
    cmd = [c for c in cmd if not (c.startswith("<") or c.startswith("["))]
    result = subprocess.run(
        [*cmd, "--help"], capture_output=True, text=True, cwd=tmp_path, check=False
    )
    assert result.returncode == 0, f"{' '.join(argv)} --help exited {result.returncode}"


def test_the_configurable_root_is_documented_with_default_and_env_name() -> None:
    text = _FRAMEWORK.read_text(encoding="utf-8")
    assert "REBAR_LLM_EVALS_CORPUS_ROOT" in text
    assert "`.rebar/evals`" in text
    assert "[llm.evals] corpus_root" in text or "`corpus_root`" in text
    assert "[tool.rebar.llm.evals]" in _EXAMPLES.read_text(encoding="utf-8")


def test_neither_doc_claims_provider_parity_writes_without_out() -> None:
    """``--out`` has no default and the write is guarded by ``if args.out:``; a doc that
    implies an automatic output location would send a client looking for a missing file."""
    section = _section_body()
    assert "--out" in section
    assert "NO default" in section or "no default" in section
    notes = _RELEASE_NOTES.read_text(encoding="utf-8")
    assert "still has NO default" in notes
