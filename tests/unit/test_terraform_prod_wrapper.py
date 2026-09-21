"""Unit tests for the mediated production Terraform wrapper."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "terraform_prod.py"


def _load_wrapper() -> Any:
    spec = importlib.util.spec_from_file_location("terraform_prod", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["terraform_prod"] = module
    spec.loader.exec_module(module)
    return module


def test_plan_checks_tree_currency_before_writing_a_saved_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = _load_wrapper()
    calls: list[tuple[list[str], Path]] = []
    checked: list[Path] = []

    def ensure_current(repo: Path, *, remote: str, branch: str, fetch: bool) -> str:
        checked.append(repo)
        assert (remote, branch, fetch) == ("origin", "main", True)
        return "a" * 40

    def run(args: list[str], *, cwd: Path) -> int:
        calls.append((args, cwd))
        return 0

    monkeypatch.setattr(wrapper, "_ensure_current", ensure_current)
    monkeypatch.setattr(wrapper, "_run", run)
    repo = tmp_path / "repo"
    terraform_root = tmp_path / "tf"
    repo.mkdir()
    terraform_root.mkdir()
    plan_path = tmp_path / "scratch" / "prod.tfplan"
    plan_path.parent.mkdir()

    rc = wrapper.main(
        [
            "plan",
            "--repo",
            str(repo),
            "--terraform-root",
            str(terraform_root),
            "--terraform-bin",
            "terraform",
            "--out",
            str(plan_path),
            "--",
            "-target=aws_lambda_function.bedrock_spend_cap",
        ]
    )

    assert rc == 0
    assert checked == [repo.resolve()]
    assert calls == [
        (["terraform", "init", "-input=false"], terraform_root.resolve()),
        (
            [
                "terraform",
                "plan",
                "-input=false",
                "-out",
                str(plan_path.resolve()),
                "-target=aws_lambda_function.bedrock_spend_cap",
            ],
            terraform_root.resolve(),
        ),
    ]
    metadata = json.loads((tmp_path / "scratch" / "prod.tfplan.rebar-meta.json").read_text())
    assert metadata["head"] == "a" * 40
    assert metadata["terraform_args"] == ["-target=aws_lambda_function.bedrock_spend_cap"]


def test_plan_rejects_a_second_out_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = _load_wrapper()
    monkeypatch.setattr(
        wrapper,
        "_ensure_current",
        lambda repo, *, remote, branch, fetch: "a" * 40,
    )
    monkeypatch.setattr(wrapper, "_run", lambda args, *, cwd: pytest.fail("terraform ran"))

    rc = wrapper.main(
        [
            "plan",
            "--repo",
            str(tmp_path),
            "--terraform-root",
            str(tmp_path),
            "--out",
            str(tmp_path / "prod.tfplan"),
            "--",
            "-out=other.tfplan",
        ]
    )

    assert rc == 2


def test_apply_rejects_a_plan_from_a_different_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = _load_wrapper()
    plan_path = tmp_path / "prod.tfplan"
    wrapper._metadata_path(plan_path).write_text(
        json.dumps({"head": "a" * 40, "terraform_root": str(tmp_path.resolve())}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        wrapper,
        "_ensure_current",
        lambda repo, *, remote, branch, fetch: "b" * 40,
    )
    monkeypatch.setattr(wrapper, "_run", lambda args, *, cwd: pytest.fail("terraform ran"))

    rc = wrapper.main(
        [
            "apply",
            "--repo",
            str(tmp_path),
            "--terraform-root",
            str(tmp_path),
            str(plan_path),
        ]
    )

    assert rc == 2


def test_apply_runs_only_wrapper_created_current_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = _load_wrapper()
    calls: list[tuple[list[str], Path]] = []
    head = "a" * 40
    plan_path = tmp_path / "prod.tfplan"
    wrapper._metadata_path(plan_path).write_text(
        json.dumps({"head": head, "terraform_root": str(tmp_path.resolve())}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        wrapper,
        "_ensure_current",
        lambda repo, *, remote, branch, fetch: head,
    )

    def run(args: list[str], *, cwd: Path) -> int:
        calls.append((args, cwd))
        return 0

    monkeypatch.setattr(wrapper, "_run", run)

    rc = wrapper.main(
        [
            "apply",
            "--repo",
            str(tmp_path),
            "--terraform-root",
            str(tmp_path),
            "--terraform-bin",
            "terraform",
            str(plan_path),
        ]
    )

    assert rc == 0
    assert calls == [
        (["terraform", "init", "-input=false"], tmp_path.resolve()),
        (["terraform", "apply", str(plan_path.resolve())], tmp_path.resolve()),
    ]
