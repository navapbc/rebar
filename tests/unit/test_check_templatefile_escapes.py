"""Guard for the ``templatefile()`` escape gate [rebar:dd30-f10d-69f3-4c36].

Commit ``ef1a7e66a65d`` added explanatory COMMENTS to ``infra/terraform/user_data.sh`` that
escaped the first mention of a bash brace expansion as ``$${...}`` and left the "reduces to"
half unescaped. ``templatefile()`` interpolates the whole file regardless of shell comment
syntax, so terraform parsed ``!PARAMS[@]`` as HCL and **every** terraform operation on the repo
failed -- ``-target`` included, because terraform evaluates the whole configuration first.

ShellCheck cannot catch this: the file is valid bash and the breakage is in another consumer.
Hence a dedicated gate, and hence these tests.

The central invariant is that the gate is **declared-variable-aware**. ``user_data.sh`` legitimately
contains four unescaped ``${data_volume_id}`` references -- the one variable ``main.tf`` passes --
so a blanket ban on ``${`` would reject the feature along with the defect.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_templatefile_escapes import (
    check_repo,
    referenced_roots,
    scan_interpolations,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_templatefile_escapes.py"


# --- the escape-aware scanner ------------------------------------------------------------- #


def test_escaped_sequence_is_not_an_interpolation() -> None:
    """``$${x}`` is a literal, not a reference. A naive grep for ``${`` cannot tell these
    apart, which is exactly why the gate does not use one."""
    assert scan_interpolations("echo $${x}") == []


def test_unescaped_sequence_is_an_interpolation() -> None:
    found = scan_interpolations("echo ${x}")
    assert [(line, body) for line, _, body in found] == [(1, "x")]


def test_percent_directive_is_scanned_and_escapable() -> None:
    assert scan_interpolations("%%{ if x }") == []
    assert len(scan_interpolations("%{ if x }")) == 1


def test_scanner_reports_the_defect_line_numbers() -> None:
    """The exact shape of the dd30 defect: first mention escaped, second not."""
    text = "a\n# consumed as $${!P[@]}, which reduces to ${!P[@]}\n"
    assert [line for line, _, _ in scan_interpolations(text)] == [2]


# --- function names are not variable references -------------------------------------------- #


def test_function_names_are_not_treated_as_variables() -> None:
    """``${jsonencode(signing_secret)}`` references ``signing_secret``, not ``jsonencode``.

    Without this, the existing ``config.js.tftpl`` would be a false positive and the gate
    would be unlandable.
    """
    assert referenced_roots("jsonencode(signing_secret)") == ["signing_secret"]


def test_attribute_access_counts_only_the_root() -> None:
    assert referenced_roots("path.module") == ["path"]


# --- the committed tree ---------------------------------------------------------------------- #


def test_committed_tree_is_clean() -> None:
    """The gate is wired into ``make lint``; the committed tree must satisfy it."""
    assert check_repo(REPO_ROOT) == []


def test_gate_exits_zero_on_the_committed_tree() -> None:
    proc = subprocess.run([sys.executable, str(GATE)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_legitimate_declared_variable_is_accepted(tmp_path: Path) -> None:
    """The load-bearing half of the contract: a declared variable must NOT be flagged.

    ``user_data.sh`` has four such references. A gate that rejected them would be rejecting
    the feature, not the defect.
    """
    (tmp_path / "main.tf").write_text(
        'x = templatefile("${path.module}/t.sh", {\n  data_volume_id = "v"\n})\n'
    )
    (tmp_path / "t.sh").write_text('echo "${data_volume_id}"\n')
    assert check_repo(tmp_path) == []


def test_undeclared_reference_is_rejected(tmp_path: Path) -> None:
    """The defect shape: a name the call site does not declare."""
    (tmp_path / "main.tf").write_text(
        'x = templatefile("${path.module}/t.sh", {\n  data_volume_id = "v"\n})\n'
    )
    (tmp_path / "t.sh").write_text("# reduces to ${!PARAMS[@]}\n")
    findings = check_repo(tmp_path)
    assert len(findings) == 1
    assert findings[0].root == "PARAMS"
    assert findings[0].line == 1


def test_escaping_the_defect_clears_the_finding(tmp_path: Path) -> None:
    """RED -> GREEN on the minimal reproduction, so the gate's teeth are pinned to the
    escaping itself and not to some incidental property of the file."""
    (tmp_path / "main.tf").write_text('x = templatefile("${path.module}/t.sh", {\n  a = "v"\n})\n')
    template = tmp_path / "t.sh"

    template.write_text("# reduces to ${!PARAMS[@]}\n")
    assert len(check_repo(tmp_path)) == 1, "unescaped form must be a finding (RED)"

    template.write_text("# reduces to $${!PARAMS[@]}\n")
    assert check_repo(tmp_path) == [], "escaped form must clear the finding (GREEN)"


def test_comments_are_not_exempt(tmp_path: Path) -> None:
    """The whole point of the bug: `#` means nothing to templatefile(). A gate that skipped
    comment lines would have let dd30 through."""
    (tmp_path / "main.tf").write_text('x = templatefile("${path.module}/t.sh", {\n  a = "v"\n})\n')
    (tmp_path / "t.sh").write_text("#!/usr/bin/env bash\n# ${UNDECLARED}\n")
    assert [f.root for f in check_repo(tmp_path)] == ["UNDECLARED"]


# --- template-path resolution (both call sites must actually be REACHED) --------------- #


def test_both_real_call_sites_resolve_to_a_real_file() -> None:
    """A guard that silently SKIPS a call site is indistinguishable from one that passes it.

    The repo has two ``templatefile()`` call sites and they use different path shapes:

    * ``main.tf`` -> ``${path.module}/user_data.sh`` (resolvable directly), and
    * ``modules/sso-gate/main.tf`` -> ``${var.source_root}/edge-gate/config.js.tftpl``, whose
      value is a MODULE INPUT supplied by a caller -- and the sso-gate module is not currently
      instantiated in the root config, so there is no caller to read it from.

    The second is the interesting one: it cannot be resolved statically, so the gate falls back
    to a unique suffix match over the tree. This test pins that BOTH resolve, so a future
    refactor cannot quietly reduce coverage to the one easy call site while still exiting 0.
    """
    from check_templatefile_escapes import _TEMPLATEFILE, resolve_template_path

    resolved: dict[str, str] = {}
    for tf_file in sorted(REPO_ROOT.rglob("*.tf")):
        parts = set(tf_file.relative_to(REPO_ROOT).parts)
        if {".venv", ".git", ".terraform"} & parts:
            continue
        text = tf_file.read_text(encoding="utf-8")
        for call in _TEMPLATEFILE.finditer(text):
            target = resolve_template_path(call.group(1), tf_file, REPO_ROOT)
            assert target is not None, (
                f"{tf_file.relative_to(REPO_ROOT)} references {call.group(1)!r} but the gate "
                "cannot resolve it -- that call site would be SILENTLY UNGUARDED"
            )
            resolved[call.group(1)] = str(target.relative_to(REPO_ROOT))

    assert resolved == {
        "${path.module}/user_data.sh": "infra/terraform/user_data.sh",
        "${var.source_root}/edge-gate/config.js.tftpl": (
            "infra/terraform/auth/edge-gate/config.js.tftpl"
        ),
    }


def test_non_literal_path_resolves_by_unique_suffix(tmp_path: Path) -> None:
    """The ``${var.X}`` fallback: strip the interpolated prefix, match the remaining suffix."""
    (tmp_path / "mod").mkdir()
    (tmp_path / "mod" / "main.tf").write_text(
        'x = templatefile("${var.source_root}/tpl/c.js.tftpl", {\n  a = "v"\n})\n'
    )
    (tmp_path / "src" / "tpl").mkdir(parents=True)
    (tmp_path / "src" / "tpl" / "c.js.tftpl").write_text("k = ${b}\n")

    findings = check_repo(tmp_path)
    assert [f.root for f in findings] == ["b"], (
        "the suffix fallback must REACH the template; an empty result here would mean the "
        "call site was skipped rather than passed"
    )


# --- wiring ------------------------------------------------------------------------------------ #


@pytest.mark.repo_policy
def test_make_lint_wires_the_gate() -> None:
    """A gate reachable only from CI can be green locally and red on the server. The sibling
    hygiene gates are wired into `make lint` for this reason; so is this one."""
    body = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "scripts/check_templatefile_escapes.py" in body, (
        "`make lint` does not invoke scripts/check_templatefile_escapes.py"
    )
