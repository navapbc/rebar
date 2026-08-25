from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import pytest

import rebar

REPO_ROOT = Path(rebar.__file__).resolve().parents[2]
LICENSE_SOURCES = (
    Path("LICENSE"),
    Path("docs/licenses/adjective-adjective-animal-LICENSE.txt"),
)
WORDLIST_SOURCES = (
    Path("src/rebar/_engine/resources/ticket-wordlist.txt"),
    Path("src/rebar/_engine/resources/ticket-wordlist-v2.txt"),
)


@pytest.fixture(scope="module")
def built_distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Build one wheel and one source archive for the distribution checks."""
    wheel_module = pytest.importorskip("hatchling.builders.wheel")
    sdist_module = pytest.importorskip("hatchling.builders.sdist")
    output = tmp_path_factory.mktemp("distribution-licenses")

    wheel_build = list(wheel_module.WheelBuilder(str(REPO_ROOT)).build(directory=str(output)))
    sdist_build = list(sdist_module.SdistBuilder(str(REPO_ROOT)).build(directory=str(output)))
    wheels = [Path(path) for path in wheel_build if str(path).endswith(".whl")]
    sdists = [Path(path) for path in sdist_build if str(path).endswith(".tar.gz")]

    assert len(wheels) == 1, f"expected one wheel, found {wheel_build}"
    assert len(sdists) == 1, f"expected one source archive, found {sdist_build}"
    return wheels[0], sdists[0]


def _wheel_license_member(names: set[str], source: Path) -> str:
    metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
    assert len(metadata) == 1, f"expected one wheel metadata file, found {metadata}"
    license_root = metadata[0].removesuffix("METADATA") + "licenses"
    return f"{license_root}/{source.as_posix()}"


def _source_archive_root(names: list[str]) -> str:
    roots = {name.split("/", 1)[0] for name in names if "/" in name}
    assert len(roots) == 1, f"expected one source archive root, found {sorted(roots)}"
    return roots.pop()


def test_wheel_contains_distribution_licenses_and_wordlists_byte_for_byte(
    built_distributions: tuple[Path, Path],
) -> None:
    wheel, _ = built_distributions
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for source in LICENSE_SOURCES:
            member = _wheel_license_member(names, source)
            assert member in names, f"wheel is missing {member}"
            assert archive.read(member) == (REPO_ROOT / source).read_bytes()
        for source in WORDLIST_SOURCES:
            member = source.relative_to("src").as_posix()
            assert member in names, f"wheel is missing {member}"
            assert archive.read(member) == (REPO_ROOT / source).read_bytes()


def test_source_archive_contains_distribution_licenses_and_wordlists_byte_for_byte(
    built_distributions: tuple[Path, Path],
) -> None:
    _, sdist = built_distributions
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        root = _source_archive_root(names)
        for source in (*LICENSE_SOURCES, *WORDLIST_SOURCES):
            member = f"{root}/{source.as_posix()}"
            assert member in names, f"source archive is missing {member}"
            extracted = archive.extractfile(member)
            assert extracted is not None, f"source archive member is not a file {member}"
            assert extracted.read() == (REPO_ROOT / source).read_bytes()
