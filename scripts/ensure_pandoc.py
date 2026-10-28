#!/usr/bin/env python3
"""Install a native-architecture pandoc when the pypandoc wheel's copy cannot run.

``pypandoc-binary==1.17`` publishes a wheel tagged ``macosx_11_0_arm64`` whose bundled
``pypandoc/files/pandoc`` is an x86_64 Mach-O executable. uv resolves the correct wheel;
its payload is wrong. On an arm64 macOS host without Rosetta that binary cannot execute,
so the wiki renderer finds no pandoc and the reconciler suite fails
(bug bb19-4776-46d2-4223; upstream context: external, JessicaTegner/pypandoc issue 447).

The trigger here is an ARCHITECTURE MISMATCH read out of the binary's own header, not a
platform name and not a CI provider. Every host whose bundled binary matches it -- all of
CI, every Linux box, x86_64 macOS -- exits having touched no network. A mismatching host
downloads the pinned upstream release, verifies the EXTRACTED binary's SHA-256 against the
pin below, and installs it under the git-ignored ``.tools/bin``, the same repo-local,
digest-verified third-party binary path the Makefile's ``actionlint-bin`` target uses.

Nothing on ``PATH`` is touched: pypandoc probes every candidate and keeps the HIGHEST
version it can execute, so a newer system pandoc would win a ``PATH`` race and break the
pandoc 3.9 pin (ADR 0095). ``PYPANDOC_PANDOC`` replaces the candidate list outright, which
is why ``tests/conftest.py`` selects this binary through that variable instead.

Run: ``make ensure-pandoc`` (or ``python scripts/ensure_pandoc.py``).
"""

from __future__ import annotations

import hashlib
import io
import platform
import struct
import sys
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PANDOC_VERSION = "3.9"

# Mach-O 64-bit little-endian magic (MH_MAGIC_64) and the CPU types we can name.
_MACHO_MAGIC_LE = b"\xcf\xfa\xed\xfe"
_MACHO_CPU_TYPES = {0x01000007: "x86_64", 0x0100000C: "arm64"}

# ELF identification and the e_machine values we can name.
_ELF_MAGIC = b"\x7fELF"
_ELF_MACHINES = {0x3E: "x86_64", 0xB7: "arm64"}

_ARCH_ALIASES = {"aarch64": "arm64", "amd64": "x86_64", "x64": "x86_64"}

Downloader = Callable[[str], bytes]


class PandocRepairError(RuntimeError):
    """The bundled pandoc cannot run here and no verified replacement was installed."""


@dataclass(frozen=True)
class Release:
    """One pinned upstream pandoc release artifact and the binary inside it."""

    url: str
    member: str
    sha256: str


# Pinned upstream artifacts, keyed by ``<system>-<arch>``. Only hosts whose bundled binary
# is for the WRONG architecture ever consult this table, so a key is present only where a
# mismatch has actually been observed and the digest measured. The darwin-arm64 digest is
# the native arm64 pandoc 3.9 that provenance already accepts
# (ACCEPTED_PANDOC_BINARY_SHA256 in scripts/generate_dc_wiki_legacy_outputs.py); a test
# asserts that membership so this pin cannot drift away from the allowlist.
RELEASES = {
    "darwin-arm64": Release(
        url=(
            "https://github.com/jgm/pandoc/releases/download/"
            f"{PANDOC_VERSION}/pandoc-{PANDOC_VERSION}-arm64-macOS.zip"
        ),
        member=f"pandoc-{PANDOC_VERSION}-arm64/bin/pandoc",
        sha256="140353e19f2518a76aa144ce90a7afcb6f485ba94d0dbe069e77c805277908c3",
    ),
}

_MANUAL_RECIPE = (
    f"Install pandoc {PANDOC_VERSION} for this architecture by hand and export PYPANDOC_PANDOC at "
    "it before running the suite; docs/local-dev-env.md carries the full recipe."
)


def normalize_arch(machine: str) -> str:
    """Return the stable architecture name for a ``platform.machine()`` style string."""
    normalized = machine.strip().lower()
    return _ARCH_ALIASES.get(normalized, normalized)


def host_key() -> str:
    """Identify this host as ``<system>-<arch>`` using the provenance vocabulary."""
    return f"{platform.system().strip().lower()}-{normalize_arch(platform.machine())}"


def read_binary_arch(path: Path) -> str | None:
    """Return the architecture named by an executable's header, or ``None`` if unreadable.

    ``None`` means "this file does not name an architecture we decode" -- a short file, a
    universal binary, a shell wrapper, a format we do not know. Callers treat that as "no
    mismatch", so an unrecognised layout degrades to leaving the host alone rather than to
    a spurious download.
    """
    try:
        header = path.read_bytes()[:20]
    except OSError:
        return None
    if len(header) < 20:
        return None
    if header[:4] == _MACHO_MAGIC_LE:
        (cpu_type,) = struct.unpack_from("<I", header, 4)
        return _MACHO_CPU_TYPES.get(cpu_type)
    if header[:4] == _ELF_MAGIC:
        endian = "<" if header[5] == 1 else ">"
        (machine,) = struct.unpack_from(f"{endian}H", header, 18)
        return _ELF_MACHINES.get(machine)
    return None


def repo_pandoc_path(repo_root: Path) -> Path:
    """Return where a repo-local pandoc is installed, whether or not it exists."""
    return repo_root / ".tools" / "bin" / "pandoc"


def repo_pandoc_for_host(repo_root: Path, arch: str | None = None) -> Path | None:
    """Return the repo-local pandoc when it exists and runs on this architecture.

    Returns ``None`` when the file is absent, and when it is present but built for a
    different architecture -- selecting such a binary would reintroduce the very failure
    this module exists to repair.
    """
    candidate = repo_pandoc_path(repo_root)
    if not candidate.is_file():
        return None
    wanted = normalize_arch(platform.machine()) if arch is None else arch
    if read_binary_arch(candidate) != wanted:
        return None
    return candidate


def bundled_pandoc_path() -> Path | None:
    """Return the pandoc bundled inside the installed pypandoc, if there is one."""
    try:
        import pypandoc
    except ImportError:
        return None
    bundled = Path(pypandoc.__file__).resolve().parent / "files" / "pandoc"
    return bundled if bundled.is_file() else None


def download(url: str) -> bytes:
    """Fetch a pinned upstream release artifact."""
    with urllib.request.urlopen(url) as response:  # fixed https GitHub release URL
        data: bytes = response.read()
    return data


def extract_member(archive: bytes, member: str) -> bytes:
    """Return one member's bytes from a zip archive held in memory."""
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        return bundle.read(member)


def install_binary(repo_root: Path, payload: bytes) -> Path:
    """Write the verified pandoc into the repo-local tool directory, executable."""
    target = repo_pandoc_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    target.chmod(0o755)
    return target


def _repair(repo_root: Path, key: str, downloader: Downloader) -> str:
    """Download, verify, and install the pinned pandoc for a mismatching host."""
    release = RELEASES.get(key)
    if release is None:
        raise PandocRepairError(
            f"the bundled pandoc cannot run on {key} and no pinned replacement is "
            f"recorded for it. {_MANUAL_RECIPE}"
        )
    payload = extract_member(downloader(release.url), release.member)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != release.sha256:
        raise PandocRepairError(
            f"refusing to install pandoc from {release.url}: the extracted binary's "
            f"SHA-256 is {actual}, not the pinned {release.sha256}. Nothing was written."
        )
    return f"installed {install_binary(repo_root, payload)} ({len(payload)} bytes)"


def ensure(repo_root: Path, *, bundled: Path | None, key: str, downloader: Downloader) -> str:
    """Make a runnable pandoc available for ``key``, returning what was done.

    Raises :class:`PandocRepairError` when a mismatch is real but cannot be repaired --
    a loud failure, because silently leaving an unrunnable binary in place is the defect
    bug bb19-4776-46d2-4223 exists to remove.
    """
    if bundled is None:
        return "pypandoc is not installed; nothing to check"
    arch = normalize_arch(key.split("-", 1)[-1])
    bundled_arch = read_binary_arch(bundled)
    if bundled_arch is None:
        return f"{bundled} names no architecture this guard decodes; leaving it alone"
    if bundled_arch == arch:
        return f"{bundled} is {arch}; no repair needed"
    existing = repo_pandoc_for_host(repo_root, arch)
    if existing is not None:
        return f"{bundled} is {bundled_arch}, not {arch}; already repaired by {existing}"
    return _repair(repo_root, key, downloader)


def main() -> int:
    """Run the architecture check for this host and report what it did."""
    try:
        outcome = ensure(
            REPO_ROOT,
            bundled=bundled_pandoc_path(),
            key=host_key(),
            downloader=download,
        )
    except (PandocRepairError, OSError, zipfile.BadZipFile) as exc:
        print(f"ensure-pandoc: {exc}", file=sys.stderr)
        return 1
    print(f"ensure-pandoc: {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
