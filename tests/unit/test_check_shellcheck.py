"""Tests for the standalone-shell ShellCheck gate [rebar:fe4e-54a5-3c3a-4901].

The gate (scripts/check_shellcheck.py) exists because of one concrete defect
class: an unguarded ``rm -rf "$dir"/*`` that expands to ``rm -rf /*`` when the
variable is empty. ShellCheck reports that as **SC2115**, at severity
``warning``.

The severity floor is therefore a behavioural contract, not a tuning knob, and
these tests pin it: a gate raised to ``-S error`` would run green over the exact
line that destroyed /opt/homebrew and /Applications on a contributor workstation.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CHK_PATH = REPO_ROOT / "scripts" / "check_shellcheck.py"

#: The incident's line, verbatim in shape: unquoted-expansion glob suffix.
VULNERABLE = '#!/bin/sh\ndir="$SOME_DIR"\nrm -rf "${dir}"/.[!.]* "${dir}"/*\n'

#: The hardened replacement recommended in the gate's own failure message.
HARDENED = (
    '#!/bin/sh\ndir="$SOME_DIR"\n: "${dir:?empty}"\n(cd -- "${dir:?}" && rm -rf -- ./*) || exit 1\n'
)


def _load():
    spec = importlib.util.spec_from_file_location("check_shellcheck", CHK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chk = _load()


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def test_shellcheck_is_a_required_tool_not_an_optional_skip():
    # Mirrors tests/unit/workflow/test_bridge_provider_wrappers_heldout.py: shellcheck-py
    # is pinned in the [dev] extra, so its absence is an environment error, never a skip.
    assert shutil.which("shellcheck") is not None, (
        "shellcheck-py is a pinned [dev] dependency, not an optional skip"
    )


def test_severity_floor_is_warning_so_sc2115_is_reachable():
    # SC2115 is emitted at `warning`. Raising this to `error` silences the single
    # finding the gate exists to catch, so the constant is pinned deliberately.
    assert chk.SEVERITY == "warning"


def test_unguarded_rm_rf_glob_fails_the_gate(tmp_path):
    repo = _repo(tmp_path, {"purge.sh": VULNERABLE})
    assert chk.main(["--root", str(repo)]) != 0


def test_unguarded_rm_rf_glob_is_reported_as_sc2115(tmp_path):
    repo = _repo(tmp_path, {"purge.sh": VULNERABLE})
    code, output = chk.run_shellcheck(repo, [Path("purge.sh")], shutil.which("shellcheck"))
    assert code != 0
    assert "SC2115" in output


def test_hardened_form_passes_the_gate(tmp_path):
    repo = _repo(tmp_path, {"purge.sh": HARDENED})
    assert chk.main(["--root", str(repo)]) == 0


def test_clean_repo_passes(tmp_path):
    repo = _repo(tmp_path, {"ok.sh": '#!/bin/sh\necho "hello"\n'})
    assert chk.main(["--root", str(repo)]) == 0


def test_repo_with_no_shell_scripts_passes(tmp_path):
    repo = _repo(tmp_path, {"README.md": "no shell here\n"})
    assert chk.discover(repo) == []
    assert chk.main(["--root", str(repo)]) == 0


def test_discover_skips_vendored_and_scratch_directories(tmp_path):
    repo = _repo(
        tmp_path,
        {
            "real.sh": "#!/bin/sh\necho ok\n",
            ".venv/bin/activate.sh": VULNERABLE,
            ".git/hooks/pre-commit.sh": VULNERABLE,
            ".claude/scratch.sh": VULNERABLE,
            ".tickets-tracker/hook.sh": VULNERABLE,
            "node_modules/pkg/install.sh": VULNERABLE,
        },
    )
    assert chk.discover(repo) == [Path("real.sh")]
    # The vulnerable copies are all excluded, so the gate stays green.
    assert chk.main(["--root", str(repo)]) == 0


def test_discover_finds_nested_scripts(tmp_path):
    repo = _repo(
        tmp_path,
        {"infra/scripts/deploy.sh": "#!/bin/sh\necho ok\n", "top.sh": "#!/bin/sh\necho ok\n"},
    )
    assert chk.discover(repo) == [Path("infra/scripts/deploy.sh"), Path("top.sh")]


def test_missing_shellcheck_fails_closed(tmp_path, monkeypatch):
    # A gate that silently skips is indistinguishable from one that passes.
    repo = _repo(tmp_path, {"ok.sh": "#!/bin/sh\necho ok\n"})
    monkeypatch.setattr(chk.shutil, "which", lambda _name: None)
    assert chk.main(["--root", str(repo)]) == 1


def test_missing_shellcheck_names_the_install_path(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, {"ok.sh": "#!/bin/sh\necho ok\n"})
    monkeypatch.setattr(chk.shutil, "which", lambda _name: None)
    chk.main(["--root", str(repo)])
    err = capsys.readouterr().err
    assert "make install" in err and "shellcheck-py" in err


def test_repository_tree_is_currently_clean():
    # The gate is wired into `make lint`; the committed tree must satisfy it.
    assert chk.main(["--root", str(REPO_ROOT)]) == 0
