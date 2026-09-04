"""The bounded Terraform structural INDEX (REB-640, slice forcible-diminished-lamb).

Turns changed/declared ``.tf``/``.tf.json`` paths into a bounded whole-module
closure: each affected directory is ONE module (all its ``.tf``/``.tf.json`` read
together); repo-contained literal local child ``source`` calls are followed
forward, and in-repo literal reverse callers are discovered. Hard bounds (64
modules, 5,000 files, 32 MiB) and repo-containment (no absolute/out-of-repo path,
no escaping symlink) are enforced BEFORE any fact is returned — a breach raises
and yields NO partial snapshot.

stdlib-only at module scope; the structural parse used for child/reverse discovery
lives in :mod:`rebar.grounding.terraform_parse` (``hcl2`` imported lazily there).
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

#: The enforced structural bounds. Read DYNAMICALLY as ``terraform_index.LIMITS``
#: (tests monkeypatch the dict) — never capture a local copy.
LIMITS: dict[str, int] = {
    "modules": 64,
    "files": 5000,
    "bytes": 33554432,
    "timeout_ms": 60000,
}

#: Directory names pruned from the whole-module closure (VCS, virtualenv, provider
#: plugin cache). A directory that holds a ``pyvenv.cfg`` is also treated as a venv.
_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {".git", ".hg", ".svn", ".terraform", ".venv", "venv", "env", "node_modules"}
)

_TF_SUFFIXES: tuple[str, ...] = (".tf", ".tf.json")
_TFVARS_SUFFIXES: tuple[str, ...] = (".tfvars", ".tfvars.json")


class TerraformPathError(ValueError):
    """A ``selected`` path is absolute, escapes the repo, or crosses an escaping symlink."""


class TerraformLimitError(RuntimeError):
    """A structural bound was exceeded; ``detail`` is the closed breach kind."""

    def __init__(self, message: str, *, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class Module:
    """One indexed module directory."""

    dir: str
    files: list[str] = field(default_factory=list)
    selected_tfvars: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Snapshot:
    """The frozen, bounded whole-module closure for a set of selected paths."""

    repo_root: str
    modules: dict[str, Module]


def _has_tf_suffix(name: str, suffixes: tuple[str, ...]) -> bool:
    return any(name.endswith(sfx) for sfx in suffixes)


def _module_dir_of(rel_posix: str) -> str:
    """The repo-relative POSIX module dir owning a file (root dir → ``.``)."""
    parent = PurePosixPath(rel_posix).parent
    return "." if str(parent) in ("", ".") else str(parent)


def _norm_rel(root: Path, target: str) -> str:
    """Resolve ``target`` under ``root`` to a repo-relative NFC POSIX path.

    Rejects NUL, absolute paths, out-of-repo paths, and escaping symlinks with
    :class:`TerraformPathError`.
    """
    if "\x00" in target:
        raise TerraformPathError(f"NUL in path {target!r}")
    norm = unicodedata.normalize("NFC", target)
    candidate = Path(norm)
    if candidate.is_absolute():
        raise TerraformPathError(f"absolute path not allowed: {target!r}")
    resolved = (root / candidate).resolve()
    root_resolved = root.resolve()
    try:
        rel = resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise TerraformPathError(f"path escapes the repo: {target!r}") from exc
    return PurePosixPath(rel).as_posix()


def _is_excluded(rel_dir: str) -> bool:
    parts = PurePosixPath(rel_dir).parts
    return any(part in _EXCLUDED_DIRS for part in parts)


def _dir_is_venv(root: Path, rel_dir: str) -> bool:
    return (root / rel_dir / "pyvenv.cfg").is_file()


def _tf_files_in_dir(root: Path, rel_dir: str) -> list[str]:
    """Sorted repo-relative ``.tf``/``.tf.json`` paths directly in ``rel_dir``."""
    base = root if rel_dir == "." else root / rel_dir
    if not base.is_dir():
        return []
    found: list[str] = []
    for name in os.listdir(base):
        if not _has_tf_suffix(name, _TF_SUFFIXES):
            continue
        if not (base / name).is_file():
            continue
        rel = name if rel_dir == "." else f"{rel_dir}/{name}"
        found.append(rel)
    return sorted(found)


def _child_dirs(root: Path, rel_dir: str, files: list[str]) -> list[str]:
    """Repo-contained literal local child module dirs called from ``files``."""
    from . import terraform_parse as tp

    out: list[str] = []
    for rel_file in files:
        parsed = _safe_parse(root, rel_file, tp)
        for call in parsed.get("module_calls", []):
            source = call.get("source")
            if not _is_local_source(source):
                continue
            child = _resolve_child_dir(root, rel_dir, source)
            if child is not None and child not in out:
                out.append(child)
    return out


def _is_local_source(source: Any) -> bool:
    return isinstance(source, str) and source.startswith((".", "..", "./", "../"))


def _resolve_child_dir(root: Path, rel_dir: str, source: str) -> str | None:
    base = PurePosixPath(rel_dir) if rel_dir != "." else PurePosixPath()
    joined = os.path.normpath(str(base / source))
    if joined.startswith("..") or os.path.isabs(joined):
        return None
    rel = PurePosixPath(joined).as_posix()
    rel = "." if rel in ("", ".") else rel
    if _is_excluded(rel) or not (root / rel).is_dir():
        return None
    if not _real_contained(root, rel):
        return None
    return rel


def _real_contained(root: Path, rel: str) -> bool:
    """True iff the symlink-resolved REAL path of ``rel`` stays within ``root``.

    A followed ``source`` whose real target escapes the repo (e.g. via a symlink
    that stays lexically ``linked/…`` but points outside) is dropped from the
    closure — silently, so the snapshot stays valid and partial-free.
    """
    try:
        root_real = os.path.realpath(root)
        target_real = os.path.realpath(root / rel)
        return os.path.commonpath([root_real, target_real]) == root_real
    except (OSError, ValueError):
        return False


def _safe_parse(root: Path, rel_file: str, tp: Any) -> dict[str, Any]:
    """Best-effort structural parse for DISCOVERY (never raises; skips on error)."""
    try:
        text = (root / rel_file).read_text(encoding="utf-8")
        return tp.parse_document(text)
    except Exception:  # noqa: BLE001 — discovery is best-effort; a bad file is skipped
        return {}


def _all_repo_tf_files(root: Path) -> list[str]:
    """Every repo ``.tf``/``.tf.json`` file, excluding VCS/venv/plugin dirs."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = PurePosixPath(Path(dirpath).relative_to(root)).as_posix()
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _EXCLUDED_DIRS and not (Path(dirpath) / d / "pyvenv.cfg").is_file()
        ]
        for name in filenames:
            if _has_tf_suffix(name, _TF_SUFFIXES):
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                out.append(rel)
    return out


def _reverse_caller_dirs(root: Path, targets: set[str]) -> list[str]:
    """In-repo literal reverse callers whose module source points into ``targets``."""
    from . import terraform_parse as tp

    out: list[str] = []
    for rel_file in _all_repo_tf_files(root):
        rel_dir = _module_dir_of(rel_file)
        if rel_dir in targets:
            continue
        parsed = _safe_parse(root, rel_file, tp)
        for call in parsed.get("module_calls", []):
            source = call.get("source")
            if not _is_local_source(source):
                continue
            child = _resolve_child_dir(root, rel_dir, source)
            if child in targets and rel_dir not in out:
                out.append(rel_dir)
    return out


def _seed_dirs(root: Path, selected: list[str]) -> tuple[set[str], dict[str, list[str]]]:
    """Normalize ``selected`` into affected module dirs + per-dir selected tfvars."""
    affected: set[str] = set()
    tfvars: dict[str, list[str]] = {}
    for raw in selected:
        rel = _norm_rel(root, raw)
        rel_dir = _module_dir_of(rel)
        if _is_excluded(rel_dir):
            raise TerraformPathError(f"selected path is in an excluded dir: {raw!r}")
        if _has_tf_suffix(rel, _TFVARS_SUFFIXES):
            affected.add(rel_dir)
            continue
        if _has_tf_suffix(rel, _TF_SUFFIXES):
            affected.add(rel_dir)
    return affected, tfvars


def _closure(root: Path, seeds: set[str]) -> dict[str, list[str]]:
    """Expand seeds forward (children) then discover reverse callers; return dir→files."""
    modules: dict[str, list[str]] = {}
    frontier = list(seeds)
    while frontier:
        rel_dir = frontier.pop()
        if rel_dir in modules or _is_excluded(rel_dir) or _dir_is_venv(root, rel_dir):
            continue
        files = _tf_files_in_dir(root, rel_dir)
        modules[rel_dir] = files
        for child in _child_dirs(root, rel_dir, files):
            if child not in modules:
                frontier.append(child)
    for caller in _reverse_caller_dirs(root, set(modules)):
        if caller not in modules and not _dir_is_venv(root, caller):
            modules[caller] = _tf_files_in_dir(root, caller)
    return modules


def _enforce_limits(root: Path, modules: dict[str, list[str]]) -> None:
    """Enforce module/file/byte bounds; raise (no partial snapshot) on breach."""
    limits = LIMITS
    if len(modules) > limits["modules"]:
        raise TerraformLimitError(
            f"{len(modules)} modules exceeds bound {limits['modules']}", detail="module_limit"
        )
    total_files = sum(len(files) for files in modules.values())
    if total_files > limits["files"]:
        raise TerraformLimitError(
            f"{total_files} files exceeds bound {limits['files']}", detail="file_limit"
        )
    total_bytes = 0
    for files in modules.values():
        for rel in files:
            try:
                total_bytes += (root / rel).stat().st_size
            except OSError:
                continue
            if total_bytes > limits["bytes"]:
                raise TerraformLimitError(
                    f"snapshot exceeds byte bound {limits['bytes']}", detail="byte_limit"
                )


def build_snapshot(repo_root: str, selected: list[str]) -> Snapshot:
    """Build the bounded whole-module closure for ``selected`` under ``repo_root``.

    Raises :class:`TerraformPathError` for an absolute/out-of-repo/escaping path and
    :class:`TerraformLimitError` (``.detail``) when a bound is breached — in either
    case NO partial snapshot is returned.
    """
    root = Path(repo_root)
    seeds, tfvars = _seed_dirs(root, selected)
    dir_files = _closure(root, seeds)
    _enforce_limits(root, dir_files)
    modules = {
        rel_dir: Module(dir=rel_dir, files=files, selected_tfvars=sorted(tfvars.get(rel_dir, [])))
        for rel_dir, files in sorted(dir_files.items())
    }
    return Snapshot(repo_root=str(root), modules=modules)


def _digest_files(root: Path, rel_paths: list[str]) -> str:
    """``sha256:`` over a sorted length-prefixed ``path+bytes`` stream."""
    import hashlib

    h = hashlib.sha256()
    for rel in sorted(rel_paths):
        path_bytes = rel.encode("utf-8")
        try:
            body = (root / rel).read_bytes()
        except OSError:
            body = b""
        h.update(len(path_bytes).to_bytes(8, "big"))
        h.update(path_bytes)
        h.update(len(body).to_bytes(8, "big"))
        h.update(body)
    return "sha256:" + h.hexdigest()


def snapshot_digest(snapshot: Snapshot) -> str:
    """Content digest over every file in the whole-module closure."""
    root = Path(snapshot.repo_root)
    all_files = [rel for module in snapshot.modules.values() for rel in module.files]
    return _digest_files(root, all_files)


def module_digest(snapshot: Snapshot, module_dir: str) -> str:
    """Content digest over one module's files (``sha256:`` of empty stream if absent)."""
    root = Path(snapshot.repo_root)
    module = snapshot.modules.get(module_dir)
    return _digest_files(root, module.files if module else [])
