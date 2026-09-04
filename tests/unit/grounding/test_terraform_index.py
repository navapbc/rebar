"""Contract oracle for the bounded Terraform structural INDEX (REB-640 / slice
forcible-diminished-lamb).

The index turns changed/declared `.tf`/`.tf.json` paths into a bounded
whole-module closure: it indexes each affected directory as one module, follows
repo-contained literal local child sources forward and discovers in-repo literal
reverse callers, and enforces hard bounds (64 modules, 5,000 files, 32 MiB) plus
repo-containment (no absolute/out-of-repo path, no escaping symlink). All
assertions are on OBSERVABLE index output, never private structure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("hcl2")
from rebar.grounding import terraform_index as tfi


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ── HAPPY PATH (given to the implementer) ────────────────────────────────────


def test_indexes_affected_directory_as_one_module(tmp_path: Path) -> None:
    _write(tmp_path, "infra/main.tf", 'variable "a" {\n  default = 1\n}\n')
    _write(tmp_path, "infra/vars.tf", 'variable "b" {\n  default = 2\n}\n')
    snap = tfi.build_snapshot(repo_root=str(tmp_path), selected=["infra/main.tf"])
    # both sibling .tf files in the affected directory are loaded together as ONE module
    assert "infra" in snap.modules
    assert set(snap.modules["infra"].files) == {"infra/main.tf", "infra/vars.tf"}


def test_declared_limits_are_the_contract_values() -> None:
    assert tfi.LIMITS["modules"] == 64
    assert tfi.LIMITS["files"] == 5000
    assert tfi.LIMITS["bytes"] == 33554432
    assert tfi.LIMITS["timeout_ms"] == 60000


# ── HELD-OUT: closure / limits / path oracle (withheld from the implementer) ─


def test_follows_local_child_module_source_forward(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "infra/main.tf",
        'module "vpc" {\n  source = "../modules/vpc"\n}\n',
    )
    _write(tmp_path, "modules/vpc/main.tf", 'output "id" {\n  value = "x"\n}\n')
    snap = tfi.build_snapshot(repo_root=str(tmp_path), selected=["infra/main.tf"])
    assert "modules/vpc" in snap.modules  # child pulled into the closure


def test_discovers_in_repo_reverse_caller(tmp_path: Path) -> None:
    _write(tmp_path, "infra/main.tf", 'module "vpc" {\n  source = "../modules/vpc"\n}\n')
    _write(tmp_path, "modules/vpc/main.tf", 'output "id" {\n  value = "x"\n}\n')
    # start from the CHILD; the caller (infra) must be discovered as a reverse caller
    snap = tfi.build_snapshot(repo_root=str(tmp_path), selected=["modules/vpc/main.tf"])
    assert "infra" in snap.modules


def test_excludes_dot_terraform_and_vcs_dirs(tmp_path: Path) -> None:
    _write(tmp_path, "infra/main.tf", 'variable "a" {\n  default = 1\n}\n')
    _write(tmp_path, "infra/.terraform/modules/x/main.tf", 'variable "vendored" {\n}\n')
    _write(tmp_path, ".git/config.tf", "not real\n")
    snap = tfi.build_snapshot(repo_root=str(tmp_path), selected=["infra/main.tf"])
    all_files = [f for m in snap.modules.values() for f in m.files]
    assert not any(".terraform" in f for f in all_files)
    assert not any(f.startswith(".git/") for f in all_files)


def test_module_limit_raises_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Shrink the module bound so the test does not need to synthesize 65 modules.
    monkeypatch.setattr(tfi, "LIMITS", {**tfi.LIMITS, "modules": 1})
    _write(tmp_path, "a/main.tf", 'module "b" {\n  source = "../b"\n}\n')
    _write(tmp_path, "b/main.tf", 'output "x" {\n  value = 1\n}\n')
    with pytest.raises(tfi.TerraformLimitError) as exc:
        tfi.build_snapshot(repo_root=str(tmp_path), selected=["a/main.tf"])
    assert exc.value.detail == "module_limit"


def test_byte_limit_raises_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tfi, "LIMITS", {**tfi.LIMITS, "bytes": 32})
    _write(tmp_path, "infra/main.tf", 'variable "a" {\n  default = "' + "x" * 200 + '"\n}\n')
    with pytest.raises(tfi.TerraformLimitError) as exc:
        tfi.build_snapshot(repo_root=str(tmp_path), selected=["infra/main.tf"])
    assert exc.value.detail == "byte_limit"


def test_absolute_or_escaping_path_raises_path_error(tmp_path: Path) -> None:
    _write(tmp_path, "infra/main.tf", 'variable "a" {\n}\n')
    with pytest.raises(tfi.TerraformPathError):
        tfi.build_snapshot(repo_root=str(tmp_path), selected=["/etc/passwd"])


def test_escaping_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_secret"
    outside.mkdir(exist_ok=True)
    (outside / "main.tf").write_text('variable "leak" {\n}\n', encoding="utf-8")
    _write(tmp_path, "infra/main.tf", 'module "e" {\n  source = "../linked"\n}\n')
    link = tmp_path / "linked"
    link.symlink_to(outside)
    snap = tfi.build_snapshot(repo_root=str(tmp_path), selected=["infra/main.tf"])
    # the escaping child is NOT pulled in — containment holds, no partial leak
    assert "linked" not in snap.modules
    all_files = [f for m in snap.modules.values() for f in m.files]
    assert not any("leak" in f or "outside" in f for f in all_files)


def test_changed_tfvars_is_inspectable_but_not_treated_as_selected(tmp_path: Path) -> None:
    _write(tmp_path, "infra/main.tf", 'variable "a" {\n  default = 1\n}\n')
    _write(tmp_path, "infra/prod.tfvars", "a = 7\n")
    snap = tfi.build_snapshot(repo_root=str(tmp_path), selected=["infra/prod.tfvars"])
    # the directory is indexed, but the tfvars is never marked as selected input
    assert "infra" in snap.modules
    assert snap.modules["infra"].selected_tfvars == []
