#!/usr/bin/env python3
"""Install and validate the hash-locked review scanner toolchain."""

import argparse
import hashlib
import json
import os
import platform as platform_mod
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_MANIFEST = REPO_ROOT / "infra" / "compose" / "review-scanners.lock.json"
DEFAULT_PREFIX = REPO_ROOT / ".tools" / "review-scanners"
SUPPORTED_PLATFORMS = frozenset({"linux/amd64", "linux/arm64"})
GITLEAKS_BIN = "gitleaks"
SEMGREP_BIN = "semgrep"

# mechanism-ok: env_var TARGETOS — aa9e-3d35 Docker BuildKit target platform selection.
# mechanism-ok: env_var TARGETARCH — aa9e-3d35 Docker BuildKit target platform selection.
# mechanism-ok: env_var RUNNER_OS — aa9e-3d35 GitHub runner platform validation fallback.
# mechanism-ok: env_var RUNNER_ARCH — aa9e-3d35 GitHub runner platform validation fallback.


class ScannerInstallError(RuntimeError):
    """A scanner lock or installation contract failed validation."""


@dataclass(frozen=True)
class PlatformEntry:
    platform: str
    gitleaks_version: str
    gitleaks_asset_arch: str
    gitleaks_url: str
    gitleaks_sha256: str
    gitleaks_checksums_url: str
    semgrep_version: str
    semgrep_requirements: Path


def _normalize_arch(arch: str) -> str:
    value = arch.strip().lower()
    aliases = {
        "x64": "amd64",
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    return aliases.get(value, value)


def _normalize_os(os_name: str) -> str:
    value = os_name.strip().lower()
    aliases = {"linux": "linux", "darwin": "darwin", "macos": "darwin", "windows": "windows"}
    return aliases.get(value, value)


def detect_platform(env: dict[str, str] | None = None) -> str:
    """Return ``os/arch`` from Docker target env, GitHub runner env, or host facts."""
    source = env if env is not None else dict(os.environ)
    if source.get("TARGETOS") and source.get("TARGETARCH"):
        return f"{_normalize_os(source['TARGETOS'])}/{_normalize_arch(source['TARGETARCH'])}"
    if source.get("RUNNER_OS") and source.get("RUNNER_ARCH"):
        return f"{_normalize_os(source['RUNNER_OS'])}/{_normalize_arch(source['RUNNER_ARCH'])}"
    return f"{_normalize_os(platform_mod.system())}/{_normalize_arch(platform_mod.machine())}"


def load_manifest(path: Path = LOCK_MANIFEST) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScannerInstallError(f"scanner lock manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ScannerInstallError(f"scanner lock manifest is invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ScannerInstallError("scanner lock manifest must be a JSON object")
    return data


def _entry_from_manifest(data: dict[str, Any], selected: str) -> PlatformEntry:
    platforms = data.get("platforms")
    if not isinstance(platforms, dict):
        raise ScannerInstallError("scanner lock manifest lacks a platforms object")
    if set(platforms) != SUPPORTED_PLATFORMS:
        raise ScannerInstallError(
            "scanner lock manifest must declare exactly " + ", ".join(sorted(SUPPORTED_PLATFORMS))
        )
    raw = platforms.get(selected)
    if not isinstance(raw, dict):
        raise ScannerInstallError(f"unsupported platform: {selected}")

    gitleaks = raw.get("gitleaks")
    semgrep = raw.get("semgrep")
    if not isinstance(gitleaks, dict) or not isinstance(semgrep, dict):
        raise ScannerInstallError(f"{selected}: scanner entry must contain gitleaks and semgrep")

    required_gitleaks = {"version", "asset_arch", "url", "sha256", "checksums_url"}
    missing = required_gitleaks - set(gitleaks)
    if missing:
        raise ScannerInstallError(f"{selected}: gitleaks entry missing {sorted(missing)}")
    digest = str(gitleaks["sha256"])
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ScannerInstallError(f"{selected}: gitleaks sha256 must be 64 lowercase hex chars")

    requirements = raw.get("semgrep_requirements") or semgrep.get("requirements")
    version = semgrep.get("version")
    if not isinstance(requirements, str) or not requirements:
        raise ScannerInstallError(f"{selected}: semgrep requirements lock is required")
    if not isinstance(version, str) or not version:
        raise ScannerInstallError(f"{selected}: semgrep version is required")
    req_path = REPO_ROOT / requirements
    return PlatformEntry(
        platform=selected,
        gitleaks_version=str(gitleaks["version"]),
        gitleaks_asset_arch=str(gitleaks["asset_arch"]),
        gitleaks_url=str(gitleaks["url"]),
        gitleaks_sha256=digest,
        gitleaks_checksums_url=str(gitleaks["checksums_url"]),
        semgrep_version=version,
        semgrep_requirements=req_path,
    )


def selected_entry(
    platform: str | None = None,
    *,
    manifest_path: Path = LOCK_MANIFEST,
    env: dict[str, str] | None = None,
) -> PlatformEntry:
    selected = platform or detect_platform(env)
    if selected not in SUPPORTED_PLATFORMS:
        raise ScannerInstallError(
            f"unsupported scanner platform: {selected}; "
            f"unsupported platform: {selected}; supported: "
            + ", ".join(sorted(SUPPORTED_PLATFORMS))
        )
    entry = _entry_from_manifest(load_manifest(manifest_path), selected)
    lock_version = semgrep_version_from_lock(entry.semgrep_requirements)
    if lock_version != entry.semgrep_version:
        raise ScannerInstallError(
            f"{selected}: manifest Semgrep {entry.semgrep_version} does not match "
            f"{entry.semgrep_requirements.name} ({lock_version or 'missing'})"
        )
    return entry


def semgrep_version_from_lock(requirements: Path) -> str | None:
    if not requirements.exists():
        raise ScannerInstallError(f"Semgrep requirements lock not found: {requirements}")
    for line in requirements.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("semgrep=="):
            return stripped.split("==", 1)[1].split()[0].rstrip("\\")
    return None


def validate_requirements_lock(requirements: Path) -> None:
    """Require every package stanza in a pip requirements lock to carry sha256 hashes."""
    if not requirements.exists():
        raise ScannerInstallError(f"Semgrep requirements lock not found: {requirements}")
    current: str | None = None
    has_hash = False
    saw_semgrep = False

    def close_current() -> None:
        if current and not has_hash:
            raise ScannerInstallError(f"{requirements}: requirement {current!r} lacks --hash")

    for raw in requirements.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash=sha256:"):
            if current is None:
                raise ScannerInstallError(f"{requirements}: hash line appears before a requirement")
            has_hash = True
            continue
        if raw[:1].isspace():
            continue
        if line.startswith("-"):
            continue
        close_current()
        current = line.split(";", 1)[0].split(" ", 1)[0].rstrip("\\")
        has_hash = "--hash=sha256:" in line
        if current.startswith("semgrep=="):
            saw_semgrep = True
    close_current()
    if not saw_semgrep:
        raise ScannerInstallError(f"{requirements}: top-level semgrep== pin is missing")


def validate_manifest(path: Path = LOCK_MANIFEST) -> None:
    data = load_manifest(path)
    versions: set[str] = set()
    for selected in sorted(SUPPORTED_PLATFORMS):
        entry = _entry_from_manifest(data, selected)
        validate_requirements_lock(entry.semgrep_requirements)
        lock_version = semgrep_version_from_lock(entry.semgrep_requirements)
        if lock_version != entry.semgrep_version:
            raise ScannerInstallError(
                f"{selected}: manifest Semgrep {entry.semgrep_version} does not match "
                f"{entry.semgrep_requirements.name} ({lock_version or 'missing'})"
            )
        versions.add(entry.semgrep_version)
    if len(versions) != 1:
        raise ScannerInstallError("all platform locks must use the same top-level Semgrep version")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_digest(path: Path, expected: str) -> None:
    actual = _sha256(path)
    if actual != expected:
        raise ScannerInstallError(
            "gitleaks sha256 mismatch (digest mismatch) for "
            f"{path}: expected {expected}, got {actual}"
        )


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, dest.open("wb") as fh:
        shutil.copyfileobj(response, fh)


def _safe_extract_gitleaks(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        member = next((m for m in tar.getmembers() if Path(m.name).name == GITLEAKS_BIN), None)
        if member is None:
            raise ScannerInstallError("Gitleaks archive does not contain a gitleaks binary")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise ScannerInstallError("Gitleaks archive entry is not a regular file")
        with extracted, (destination / GITLEAKS_BIN).open("wb") as fh:
            shutil.copyfileobj(extracted, fh)
    binary = destination / GITLEAKS_BIN
    binary.chmod(0o755)
    return binary


def _venv_bin(venv_dir: Path, name: str) -> Path:
    scripts = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return venv_dir / scripts / f"{name}{suffix}"


def _pip_install_command(pip: Path, requirements: Path) -> list[str]:
    return [
        str(pip),
        "install",
        "--require-hashes",
        "--no-cache-dir",
        "-r",
        str(requirements),
    ]


def _install_semgrep(entry: PlatformEntry, venv_dir: Path) -> Path:
    validate_requirements_lock(entry.semgrep_requirements)
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    command = _pip_install_command(_venv_bin(venv_dir, "pip"), entry.semgrep_requirements)
    try:
        subprocess.run(command, check=True)  # raw-git-ok: hash-locked pip, not git/tracker.
    except subprocess.CalledProcessError as exc:
        raise ScannerInstallError("Semgrep hash-locked pip install failed") from exc
    return _venv_bin(venv_dir, SEMGREP_BIN)


def _run_version(binary: Path, expected: str) -> None:
    try:
        proc = subprocess.run(
            [str(binary), "version" if binary.name == GITLEAKS_BIN else "--version"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ScannerInstallError(f"could not execute {binary}: {exc}") from exc
    output = proc.stdout.strip()
    found = re.search(r"\d+\.\d+\.\d+", output)
    if not found or found.group(0) != expected:
        raise ScannerInstallError(
            f"{binary.name} version mismatch: expected {expected}, got {output!r}"
        )


def _install_gitleaks(
    entry: PlatformEntry, prefix: Path, archive_override: Path | None = None
) -> Path:
    work = prefix / "work"
    archive = work / f"gitleaks-{entry.gitleaks_version}-{entry.gitleaks_asset_arch}.tar.gz"
    extract_dir = work / "gitleaks-extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    if archive_override is None:
        _download(entry.gitleaks_url, archive)
    else:
        archive = archive_override
    _verify_digest(archive, entry.gitleaks_sha256)
    binary = _safe_extract_gitleaks(archive, extract_dir)
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / GITLEAKS_BIN
    shutil.copy2(binary, target)
    target.chmod(0o755)
    return target


def _link_semgrep(semgrep: Path, prefix: Path) -> Path:
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / SEMGREP_BIN
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        link.symlink_to(semgrep)
    except OSError:
        shutil.copy2(semgrep, link)
        link.chmod(0o755)
    return link


def install_scanners(
    entry: PlatformEntry,
    prefix: Path,
    *,
    gitleaks_archive: Path | None = None,
    semgrep_venv: Path | None = None,
) -> None:
    prefix.mkdir(parents=True, exist_ok=True)
    gitleaks = _install_gitleaks(entry, prefix, gitleaks_archive)
    semgrep = _install_semgrep(entry, semgrep_venv or prefix / "semgrep-venv")
    semgrep_link = _link_semgrep(semgrep, prefix)
    _run_version(gitleaks, entry.gitleaks_version)
    _run_version(semgrep_link, entry.semgrep_version)


def check_scanners(entry: PlatformEntry, prefix: Path) -> None:
    validate_requirements_lock(entry.semgrep_requirements)
    _run_version(prefix / "bin" / GITLEAKS_BIN, entry.gitleaks_version)
    _run_version(prefix / "bin" / SEMGREP_BIN, entry.semgrep_version)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("install", "check", "validate-locks"))
    parser.add_argument("--platform", help="scanner platform as os/arch, e.g. linux/amd64")
    parser.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX)
    parser.add_argument("--manifest", "--lock", dest="manifest", type=Path, default=LOCK_MANIFEST)
    parser.add_argument("--install-dir", type=Path)
    parser.add_argument("--semgrep-venv", type=Path)
    parser.add_argument("--gitleaks-archive", type=Path)
    parser.add_argument("--check-only", action="store_true")
    return parser.parse_args(argv)


def _selected_prefix(args: argparse.Namespace) -> Path:
    if args.install_dir is not None:
        return args.install_dir.parent
    return args.prefix


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        validate_manifest(args.manifest)
        mode = args.mode or ("check" if args.check_only else None)
        if mode is None:
            raise ScannerInstallError("mode is required unless --check-only is supplied")
        if mode == "validate-locks":
            print("review scanner locks: OK")
            return 0
        entry = selected_entry(args.platform, manifest_path=args.manifest)
        if args.check_only and args.gitleaks_archive is not None:
            _verify_digest(args.gitleaks_archive, entry.gitleaks_sha256)
            print(f"review scanners check-only: OK ({entry.platform})")
            return 0
        if args.check_only:
            check_scanners(entry, _selected_prefix(args))
            print(f"review scanners check-only: OK ({entry.platform})")
            return 0
        prefix = _selected_prefix(args)
        if mode == "install":
            install_scanners(
                entry,
                prefix,
                gitleaks_archive=args.gitleaks_archive,
                semgrep_venv=args.semgrep_venv,
            )
        else:
            check_scanners(entry, prefix)
        print(f"review scanners {args.mode}: OK ({entry.platform})")
        print(f"PATH prefix: {prefix / 'bin'}")
        return 0
    except ScannerInstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
