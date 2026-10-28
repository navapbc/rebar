"""Cover the architecture-triggered pandoc repair (bug bb19-4776-46d2-4223).

``pypandoc-binary``'s arm64 macOS wheel bundles an x86_64 executable, so the repair is
driven by reading the bundled binary's own header rather than by a platform name. These
tests inject the download boundary; none of them reaches the network.
"""

from __future__ import annotations

import hashlib
import io
import struct
import zipfile
from pathlib import Path

import ensure_pandoc
import pytest
from generate_dc_wiki_legacy_outputs import ACCEPTED_PANDOC_BINARY_SHA256

pytestmark = pytest.mark.scripts

_MEMBER = "pandoc-3.9-arm64/bin/pandoc"


def _macho(cpu_type: int) -> bytes:
    """Build a Mach-O 64-bit little-endian header naming one CPU type."""
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu_type) + b"\x00" * 12


def _elf(machine: int) -> bytes:
    """Build a little-endian 64-bit ELF header naming one e_machine."""
    return b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + struct.pack("<HH", 2, machine)


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _zip_with(payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(_MEMBER, payload)
    return buffer.getvalue()


def _release_for(payload: bytes) -> ensure_pandoc.Release:
    return ensure_pandoc.Release(
        url="https://example.invalid/pandoc.zip",
        member=_MEMBER,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _exploding_downloader(url: str) -> bytes:
    raise AssertionError(f"no download expected, but {url} was requested")


def test_matching_architecture_repairs_nothing_and_never_downloads(tmp_path: Path) -> None:
    """A bundled binary built for the host needs no repair and no network access."""
    bundled = _write(tmp_path / "files" / "pandoc", _macho(0x0100000C))

    outcome = ensure_pandoc.ensure(
        tmp_path,
        bundled=bundled,
        key="darwin-arm64",
        downloader=_exploding_downloader,
    )

    assert "no repair needed" in outcome
    assert not ensure_pandoc.repo_pandoc_path(tmp_path).exists()


def test_mismatched_architecture_installs_the_verified_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An x86_64 payload on an arm64 host is replaced by the pinned native binary."""
    bundled = _write(tmp_path / "files" / "pandoc", _macho(0x01000007))
    payload = _macho(0x0100000C) + b"native-pandoc"
    archive = _zip_with(payload)
    monkeypatch.setattr(
        ensure_pandoc, "RELEASES", {"darwin-arm64": _release_for(payload)}, raising=True
    )

    outcome = ensure_pandoc.ensure(
        tmp_path,
        bundled=bundled,
        key="darwin-arm64",
        downloader=lambda url: archive,
    )

    installed = ensure_pandoc.repo_pandoc_path(tmp_path)
    assert "installed" in outcome
    assert installed.read_bytes() == payload
    assert installed.stat().st_mode & 0o111


def test_digest_mismatch_refuses_and_installs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload that fails the pin is rejected rather than installed unverified."""
    bundled = _write(tmp_path / "files" / "pandoc", _macho(0x01000007))
    monkeypatch.setattr(
        ensure_pandoc,
        "RELEASES",
        {"darwin-arm64": _release_for(b"the-binary-we-pinned")},
        raising=True,
    )

    with pytest.raises(ensure_pandoc.PandocRepairError, match="SHA-256"):
        ensure_pandoc.ensure(
            tmp_path,
            bundled=bundled,
            key="darwin-arm64",
            downloader=lambda url: _zip_with(b"something-else-entirely"),
        )

    assert not ensure_pandoc.repo_pandoc_path(tmp_path).exists()


def test_unrepairable_host_fails_loudly_instead_of_degrading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mismatch with no pinned replacement raises rather than leaving a broken binary."""
    bundled = _write(tmp_path / "files" / "pandoc", _elf(0x3E))
    monkeypatch.setattr(ensure_pandoc, "RELEASES", {}, raising=True)

    with pytest.raises(ensure_pandoc.PandocRepairError, match="linux-arm64"):
        ensure_pandoc.ensure(
            tmp_path,
            bundled=bundled,
            key="linux-arm64",
            downloader=_exploding_downloader,
        )


def test_already_repaired_host_skips_the_download(tmp_path: Path) -> None:
    """A native binary already under .tools/bin is reused, not re-fetched."""
    bundled = _write(tmp_path / "files" / "pandoc", _macho(0x01000007))
    _write(ensure_pandoc.repo_pandoc_path(tmp_path), _macho(0x0100000C))

    outcome = ensure_pandoc.ensure(
        tmp_path,
        bundled=bundled,
        key="darwin-arm64",
        downloader=_exploding_downloader,
    )

    assert "already repaired" in outcome


def test_absent_pypandoc_is_not_an_error(tmp_path: Path) -> None:
    """An install without the wiki extra has no bundled binary to check."""
    outcome = ensure_pandoc.ensure(
        tmp_path,
        bundled=None,
        key="darwin-arm64",
        downloader=_exploding_downloader,
    )

    assert "not installed" in outcome


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_macho(0x01000007), "x86_64"),
        (_macho(0x0100000C), "arm64"),
        (_elf(0x3E), "x86_64"),
        (_elf(0xB7), "arm64"),
        (b"#!/bin/sh\necho hello\n", None),
        (b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x0100000F) + b"\x00" * 12, None),
        (b"short", None),
    ],
)
def test_read_binary_arch_decodes_or_abstains(
    tmp_path: Path, payload: bytes, expected: str | None
) -> None:
    """Known headers decode; anything else abstains so no spurious repair is triggered."""
    assert ensure_pandoc.read_binary_arch(_write(tmp_path / "candidate", payload)) == expected


def test_read_binary_arch_abstains_on_an_unreadable_path(tmp_path: Path) -> None:
    """A missing file abstains rather than raising out of the bootstrap."""
    assert ensure_pandoc.read_binary_arch(tmp_path / "absent") is None


def test_repo_pandoc_for_host_rejects_absent_and_foreign_binaries(tmp_path: Path) -> None:
    """Selection returns nothing unless a repo-local binary matches this architecture."""
    assert ensure_pandoc.repo_pandoc_for_host(tmp_path, "arm64") is None

    installed = _write(ensure_pandoc.repo_pandoc_path(tmp_path), _macho(0x01000007))

    assert ensure_pandoc.repo_pandoc_for_host(tmp_path, "arm64") is None
    assert ensure_pandoc.repo_pandoc_for_host(tmp_path, "x86_64") == installed


def test_pinned_digest_is_accepted_by_pandoc_provenance() -> None:
    """The artifact this bootstrap installs must already satisfy the provenance allowlist.

    Otherwise a repaired host would install a pandoc the wiki corpus verification then
    rejects, trading one broken bootstrap for another.
    """
    pinned = ensure_pandoc.RELEASES["darwin-arm64"].sha256

    assert pinned in ACCEPTED_PANDOC_BINARY_SHA256["darwin-arm64"]


def test_normalize_arch_folds_the_vendor_spellings() -> None:
    """uname spellings must land on the same vocabulary the pin table is keyed by."""
    assert ensure_pandoc.normalize_arch("aarch64") == "arm64"
    assert ensure_pandoc.normalize_arch("AMD64") == "x86_64"
    assert ensure_pandoc.normalize_arch("arm64") == "arm64"
