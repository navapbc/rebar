"""Unit tests for the `rebar llm setup` wizard (WS-J2)."""

from __future__ import annotations

import json

from rebar._cli import main


def test_setup_text_reports_and_validates(capsys) -> None:
    rc = main(["llm", "setup"])
    out = capsys.readouterr().out
    assert "rebar LLM setup" in out
    assert "extras:" in out
    assert "FakeRunner dry-run:" in out
    assert "[tool.rebar.llm]" in out
    # The FakeRunner dry-run validates offline (no tokens), so setup succeeds.
    assert rc == 0


def test_setup_json_shape(capsys) -> None:
    rc = main(["llm", "setup", "--output", "json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) >= {"extras", "dry_run_ok", "recommended_config"}
    assert report["dry_run_ok"] is True
    assert report["extras"].keys() >= {"agents", "eval", "tracing"}
    assert "[tool.rebar.llm]" in report["recommended_config"]


def test_setup_writes_config(tmp_path, capsys) -> None:
    dest = tmp_path / "llm.toml"
    rc = main(["llm", "setup", "--write", str(dest)])
    assert rc == 0
    assert "Wrote" in capsys.readouterr().out
    assert "[tool.rebar.llm]" in dest.read_text()


def test_setup_no_subcommand_prints_help(capsys) -> None:
    rc = main(["llm"])
    assert rc == 1
    assert "setup" in capsys.readouterr().out


def test_setup_configures_otlp_endpoint(capsys) -> None:
    rc = main(["llm", "setup", "--otlp-endpoint", "http://collector:4317", "--output", "json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["otlp_endpoint"] == "http://collector:4317"
    assert "[tool.rebar.llm.tracing]" in report["recommended_config"]
    assert "http://collector:4317" in report["recommended_config"]
