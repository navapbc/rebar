"""Tests for the README-quickstart golden-path extractor+runner (ticket 0435).

The checker (scripts/check_readme_quickstart.py) extracts the bash fenced block under
the README '## Quickstart' heading and executes those EXACT lines end-to-end in a
throwaway git repo, so a wrong command *printed* in the README fails the build.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.scripts

REPO_ROOT = Path(__file__).resolve().parents[2]
CHK_PATH = REPO_ROOT / "scripts" / "check_readme_quickstart.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_readme_quickstart", CHK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chk = _load()


_SYNTHETIC = """# Demo

## Install

```bash
pipx install nava-rebar
```

## Quickstart

```bash
rebar init
tid=$(rebar create task "x" | tail -1)
rebar claim "$tid" --assignee alice
```
```python
import rebar
```

## Next
"""


# ─────────────────────────── HAPPY PATH (shown to implementer) ────────────────


def test_extract_real_readme_has_golden_path_commands():
    """The real README quickstart block contains the full CLI golden path."""
    block = chk.extract_quickstart_bash((REPO_ROOT / "README.md").read_text())
    for needle in ("rebar init", "rebar create", "rebar claim", "rebar transition"):
        assert needle in block, f"{needle!r} missing from extracted quickstart"


def test_main_runs_real_quickstart_green():
    """main() extracts the real README quickstart and runs it end-to-end (exit 0)."""
    assert chk.main([]) == 0


# ─────────────────────────── EDGE CASES (HELD OUT) ────────────────────────────


def test_extract_selects_quickstart_not_install_block():
    """Extraction picks the bash block under '## Quickstart', not the earlier Install
    block (which also uses ```bash)."""
    block = chk.extract_quickstart_bash(_SYNTHETIC)
    assert "rebar init" in block
    assert "pipx install" not in block  # the Install block must not be captured


def test_extract_returns_bash_only_not_python():
    """Only the bash block is returned — the adjacent python block is excluded."""
    block = chk.extract_quickstart_bash(_SYNTHETIC)
    assert "import rebar" not in block


def test_extract_captures_id_pipeline():
    """The captured block keeps the id-capture pattern (no hard-coded id)."""
    block = chk.extract_quickstart_bash(_SYNTHETIC)
    assert 'tid=$(rebar create task "x" | tail -1)' in block
    assert 'rebar claim "$tid"' in block


def test_extract_raises_when_no_quickstart(tmp_path: Path):
    """A README with no '## Quickstart' bash block is a hard error (not silent empty)."""
    with pytest.raises((ValueError, LookupError)):
        chk.extract_quickstart_bash("# Doc\n\n## Other\n\nno block here\n")


# ─────────────────────────── E2E via main() (HELD OUT) ────────────────────────


def _write_readme(tmp_path: Path, quickstart_bash: str) -> Path:
    body = f"# Demo\n\n## Quickstart\n\n```bash\n{quickstart_bash}\n```\n"
    p = tmp_path / "README.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_main_fails_on_broken_quickstart_command(tmp_path: Path, monkeypatch):
    """A wrong command printed in the README quickstart makes main() exit non-zero.
    Here: the historical session-log defect — the bare form without the `append` verb."""
    readme = _write_readme(
        tmp_path,
        'rebar init\nrebar session-log "note"\n',  # invalid: missing the `append` verb
    )
    monkeypatch.setattr(chk, "DEFAULT_README", readme, raising=False)
    assert chk.main([]) != 0


def test_main_passes_on_valid_quickstart(tmp_path: Path, monkeypatch):
    """A valid runnable quickstart block makes main() exit 0."""
    readme = _write_readme(
        tmp_path,
        'rebar init\ntid=$(rebar create task "x" | tail -1)\n'
        'rebar claim "$tid" --assignee alice\n'
        'rebar transition "$tid" in_progress closed\n',
    )
    monkeypatch.setattr(chk, "DEFAULT_README", readme, raising=False)
    assert chk.main([]) == 0
