"""Unit tests for the `rebar workflow` CLI (WS-B4): new / validate / --dry-run.

Drive ``rebar._cli.main`` in-process; the workflow arms touch no store, so no
init/network is needed. ``new`` always writes to a tmp path here so the repo's
.rebar/workflows is never touched.
"""

from __future__ import annotations

import json

import pytest

from rebar._cli import main
from rebar.llm.workflow import lint as L
from rebar.llm.workflow import scaffold


def test_scaffold_is_valid() -> None:
    pytest.importorskip("jsonschema")
    findings = L.lint_workflow(scaffold("demo"), source="demo")
    assert findings == [], "\n".join(str(f) for f in findings)


def test_scaffold_rejects_bad_name() -> None:
    from rebar.llm.errors import WorkflowParseError

    with pytest.raises(WorkflowParseError, match="invalid workflow name"):
        scaffold("Bad Name")


def test_new_to_stdout(capsys) -> None:
    rc = main(["workflow", "new", "myflow", "-o", "-"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "schema_version:" in out
    assert "name: myflow" in out
    assert "yaml-language-server: $schema=" in out


def test_new_writes_file_and_refuses_overwrite(tmp_path, capsys) -> None:
    dest = tmp_path / "wf.yaml"
    rc = main(["workflow", "new", "wf", "-o", str(dest)])
    assert rc == 0
    assert dest.exists()
    capsys.readouterr()
    # Second time without --force fails.
    rc = main(["workflow", "new", "wf", "-o", str(dest)])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err
    # With --force succeeds.
    rc = main(["workflow", "new", "wf", "-o", str(dest), "--force"])
    assert rc == 0


def test_new_invalid_name_exits_1(capsys) -> None:
    rc = main(["workflow", "new", "Bad Name", "-o", "-"])
    assert rc == 1
    assert "invalid workflow name" in capsys.readouterr().err


def test_validate_good_file(tmp_path, capsys) -> None:
    pytest.importorskip("jsonschema")
    f = tmp_path / "good.yaml"
    f.write_text(scaffold("good"))
    rc = main(["workflow", "validate", str(f)])
    assert rc == 0
    assert "is valid" in capsys.readouterr().out


def test_validate_bad_file_exits_1(tmp_path, capsys) -> None:
    f = tmp_path / "bad.yaml"
    f.write_text(
        'schema_version: "1"\nname: bad\nsteps:\n'
        "  - id: a\n    uses: u\n    with:\n      v: ${{ inputs.nope }}\n"
    )
    rc = main(["workflow", "validate", str(f)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "undeclared workflow input 'nope'" in out


def test_validate_json_output(tmp_path, capsys) -> None:
    f = tmp_path / "bad.yaml"
    f.write_text(
        'schema_version: "1"\nname: bad\nsteps:\n  - id: a\n    uses: u\n    needs: [ghost]\n'
    )
    rc = main(["workflow", "validate", str(f), "--output", "json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["source"] == str(f)
    assert any("ghost" in fi["message"] for fi in payload["findings"])


def test_validate_dry_run_banner(tmp_path, capsys) -> None:
    pytest.importorskip("jsonschema")
    f = tmp_path / "good.yaml"
    f.write_text(scaffold("good"))
    rc = main(["workflow", "validate", str(f), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "no LLM calls" in out


def test_validate_missing_file(capsys) -> None:
    rc = main(["workflow", "validate", "/no/such/file.yaml"])
    assert rc == 1
    assert "cannot read" in capsys.readouterr().err


def test_validate_expressions_off(tmp_path, capsys) -> None:
    f = tmp_path / "x.yaml"
    f.write_text(
        'schema_version: "1"\nname: x\ninputs:\n  t: {type: string}\n'
        "steps:\n  - id: a\n    uses: u\n    with:\n      v: ${{ inputs.t }}\n"
    )
    rc = main(["workflow", "validate", str(f), "--no-expressions"])
    assert rc == 1
    assert "expressions are disabled" in capsys.readouterr().out
