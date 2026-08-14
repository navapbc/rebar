"""Unit contracts for the deterministic DC wiki legacy-output generator.

These tests exercise generation mechanics with fakes. The committed fixture and
``test_wiki_render_hardening.py`` retain the real pinned-Pandoc evidence; this file
must not pay for another 884 conversions in the ordinary suite.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.scripts

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_dc_wiki_legacy_outputs.py"


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_dc_wiki_legacy_outputs", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_conversion_contract_is_independent_of_live_constants(
    generator: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A production format drift must not move the historical generator with it."""
    monkeypatch.setattr(generator.wiki_render, "_PANDOC_FROM", "markdown")
    monkeypatch.setattr(generator.wiki_render, "_PANDOC_TO", "plain")
    monkeypatch.setattr(generator.wiki_render, "_PANDOC_ARGS", ("--standalone",))
    observed: dict[str, object] = {}

    def fake_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="h1. {anchor:generated}Heading\n",
            stderr="",
        )

    rendered = generator.legacy_convert("# Heading", "/pinned/pandoc", runner=fake_runner)

    assert observed["argv"] == [
        "/pinned/pandoc",
        "-f",
        "commonmark",
        "-t",
        "jira",
        "--wrap=none",
    ]
    assert observed["kwargs"] == {
        "input": "# Heading",
        "capture_output": True,
        "text": True,
        "timeout": 30,
        "check": False,
    }
    assert rendered == "h1. Heading"


@pytest.mark.parametrize("failure", ["nonzero", "spawn"])
def test_legacy_conversion_records_failures_as_none(
    generator: ModuleType,
    failure: str,
) -> None:
    def fake_runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if failure == "spawn":
            raise OSError("unspawnable")
        return subprocess.CompletedProcess(argv, 3, stdout="partial", stderr="bad input")

    assert generator.legacy_convert("body", "/pandoc", runner=fake_runner) is None


def test_build_fixture_pins_ordered_inputs_outputs_and_binary(
    generator: ModuleType,
    tmp_path: Path,
) -> None:
    pandoc = tmp_path / "pandoc"
    pandoc.write_bytes(b"fixed binary bytes")
    binary_sha256 = generator.SUPPORTED_PANDOC_BINARY_SHA256["darwin-arm64"]
    units = ["first unit", "second → unit", "first unit"]
    calls: list[tuple[str, str]] = []

    def fake_convert(prepared: str, binary: str) -> str | None:
        calls.append((prepared, binary))
        return None if prepared.startswith("second") else prepared.upper()

    fixture = generator.build_fixture(
        units,
        pandoc=str(pandoc),
        pandoc_version="3.6.1",
        generating_platform="darwin-arm64",
        converter=fake_convert,
        binary_hasher=lambda _path: binary_sha256,
    )

    assert calls == [(unit, str(pandoc)) for unit in units]
    assert fixture["pandoc"] == {
        "version": "3.6.1",
        "generating_platform": "darwin-arm64",
        "binary_sha256": binary_sha256,
        "supported_platform_binary_sha256": generator.SUPPORTED_PANDOC_BINARY_SHA256,
    }
    assert fixture["conversion"] == {
        "from": "commonmark",
        "to": "jira",
        "args": ["--wrap=none"],
        "postprocess": "strip-pandoc-anchor-macros-and-trailing-newlines",
    }
    assert fixture["unit_count"] == 3
    assert fixture["output_encoding"] == "utf-8/base85"
    assert fixture["units"] == [
        {
            "input_sha256": hashlib.sha256(unit.encode("utf-8")).hexdigest(),
            "output_b85": (
                None
                if unit.startswith("second")
                else base64.b85encode(unit.upper().encode("utf-8")).decode("ascii")
            ),
        }
        for unit in units
    ]


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "darwin-arm64"),
        ("Linux", "AMD64", "linux-x86_64"),
        ("Linux", "x86_64", "linux-x86_64"),
    ],
)
def test_platform_keys_are_stable(
    generator: ModuleType,
    system: str,
    machine: str,
    expected: str,
) -> None:
    assert generator.stable_platform_key(system, machine) == expected


@pytest.mark.parametrize("generating_platform", ["darwin-arm64", "linux-x86_64"])
def test_regeneration_on_either_supported_platform_retains_the_complete_map(
    generator: ModuleType,
    generating_platform: str,
) -> None:
    fingerprints = generator.SUPPORTED_PANDOC_BINARY_SHA256

    fixture = generator.build_fixture(
        [],
        pandoc="/pinned/pandoc",
        pandoc_version=generator.LEGACY_PANDOC_VERSION,
        generating_platform=generating_platform,
        binary_hasher=lambda _path: fingerprints[generating_platform],
    )

    assert fixture["pandoc"] == {
        "version": generator.LEGACY_PANDOC_VERSION,
        "generating_platform": generating_platform,
        "binary_sha256": fingerprints[generating_platform],
        "supported_platform_binary_sha256": fingerprints,
    }


@pytest.mark.parametrize("platform_key", ["darwin-arm64", "linux-x86_64"])
def test_validation_accepts_each_supported_platform(
    generator: ModuleType,
    platform_key: str,
) -> None:
    fingerprints = generator.SUPPORTED_PANDOC_BINARY_SHA256
    provenance = {
        "version": generator.LEGACY_PANDOC_VERSION,
        "generating_platform": "darwin-arm64",
        "binary_sha256": fingerprints["darwin-arm64"],
        "supported_platform_binary_sha256": fingerprints,
    }

    generator.validate_pandoc_provenance(
        provenance,
        platform_key=platform_key,
        version="3.6.1",
        binary_sha256=fingerprints[platform_key],
    )


@pytest.mark.parametrize(
    ("platform_key", "version", "binary_sha256", "message"),
    [
        ("windows-x86_64", "3.6.1", "0" * 64, "unsupported Pandoc platform"),
        ("linux-x86_64", "3.7", "0" * 64, "Pandoc version mismatch"),
        ("linux-x86_64", "3.6.1", "0" * 64, "Pandoc binary SHA-256 mismatch"),
    ],
)
def test_validation_rejects_platform_version_and_hash_drift_with_diagnostics(
    generator: ModuleType,
    platform_key: str,
    version: str,
    binary_sha256: str,
    message: str,
) -> None:
    fingerprints = generator.SUPPORTED_PANDOC_BINARY_SHA256
    provenance = {
        "version": generator.LEGACY_PANDOC_VERSION,
        "generating_platform": "darwin-arm64",
        "binary_sha256": fingerprints["darwin-arm64"],
        "supported_platform_binary_sha256": fingerprints,
    }

    with pytest.raises(ValueError, match=message):
        generator.validate_pandoc_provenance(
            provenance,
            platform_key=platform_key,
            version=version,
            binary_sha256=binary_sha256,
        )


def test_validation_rejects_an_incomplete_supported_platform_map(
    generator: ModuleType,
) -> None:
    fingerprints = generator.SUPPORTED_PANDOC_BINARY_SHA256
    provenance = {
        "version": generator.LEGACY_PANDOC_VERSION,
        "generating_platform": "darwin-arm64",
        "binary_sha256": fingerprints["darwin-arm64"],
        "supported_platform_binary_sha256": {
            "darwin-arm64": fingerprints["darwin-arm64"],
        },
    }

    with pytest.raises(ValueError, match="fingerprint map is incomplete"):
        generator.validate_pandoc_provenance(
            provenance,
            platform_key="darwin-arm64",
            version="3.6.1",
            binary_sha256=fingerprints["darwin-arm64"],
        )


def test_serialization_is_compact_canonical_and_byte_deterministic(generator: ModuleType) -> None:
    first = {
        "units": [{"output_b85": "encoded", "input_sha256": "a" * 64}],
        "schema_version": 1,
    }
    second = {
        "schema_version": 1,
        "units": [{"input_sha256": "a" * 64, "output_b85": "encoded"}],
    }

    encoded_first = generator.serialize_fixture(first)
    encoded_second = generator.serialize_fixture(second)

    assert encoded_first == encoded_second
    assert encoded_first.endswith(b"\n")
    assert b"\n " not in encoded_first
    assert json.loads(encoded_first) == second


def test_generator_prepares_the_exact_committed_order_without_running_pandoc(
    generator: ModuleType,
) -> None:
    units = generator.renderable_units()

    assert len(units) == 884
    assert units[0]
    assert hashlib.sha256("".join(units).encode("utf-8")).hexdigest() == (
        "f7b2c4d4300df8f2ae60c345cb1c5a4e332c96ba6e453840015c6954467c623b"
    )


def test_replay_fixture_captures_every_required_settling_conversion(
    generator: ModuleType,
) -> None:
    outputs = {
        "first": "FIRST",
        "FIRST": "FIRST",
        "second": "SECOND-1",
        "SECOND-1": "SECOND-2",
        "SECOND-2": "SECOND-2",
    }

    def fake_convert(markdown: str, _pandoc: str, _timeout: float | None = None) -> str:
        return outputs[markdown]

    recorder = generator.ReplayRecorder(fake_convert)

    def render(markdown: str) -> str:
        return recorder(markdown, "/pinned/pandoc", 30.0)

    fixture = generator.build_replay_fixture(
        "prose",
        ["first", "second"],
        render=render,
        recorder=recorder,
    )

    assert fixture["schema_version"] == 1
    assert fixture["stratum"] == "prose"
    assert fixture["body_count"] == 2
    assert fixture["pandoc"] == {
        "version": generator.LEGACY_PANDOC_VERSION,
        "supported_platform_binary_sha256": generator.SUPPORTED_PANDOC_BINARY_SHA256,
    }
    assert fixture["conversion_count"] == 5
    assert fixture["conversion_trace"] == [
        hashlib.sha256(value.encode("utf-8")).hexdigest()
        for value in ["first", "FIRST", "second", "SECOND-1", "SECOND-2"]
    ]
    assert fixture["bodies"][0]["pass_output_sha256"] == [
        hashlib.sha256(value.encode("utf-8")).hexdigest() for value in ["FIRST", "FIRST"]
    ]
    assert fixture["bodies"][1]["pass_output_sha256"] == [
        hashlib.sha256(value.encode("utf-8")).hexdigest()
        for value in ["SECOND-1", "SECOND-2", "SECOND-2"]
    ]


def test_replay_recorder_rejects_nondeterministic_output_for_the_same_input(
    generator: ModuleType,
) -> None:
    outputs = iter(["first output", "different output"])

    def inconsistent_convert(
        _markdown: str,
        _pandoc: str,
        _timeout: float | None = None,
    ) -> str:
        return next(outputs)

    recorder = generator.ReplayRecorder(inconsistent_convert)

    assert recorder("same prepared input", "/pinned/pandoc", 30.0) == "first output"
    with pytest.raises(
        ValueError,
        match="Pandoc produced different bytes for the same prepared input",
    ):
        recorder("same prepared input", "/pinned/pandoc", 30.0)


def test_replay_fixture_rejects_a_body_that_does_not_settle_by_pass_three(
    generator: ModuleType,
) -> None:
    outputs = {
        "source": "pass one",
        "pass one": "pass two",
        "pass two": "pass three",
    }

    def fake_convert(markdown: str, _pandoc: str, _timeout: float | None = None) -> str:
        return outputs[markdown]

    recorder = generator.ReplayRecorder(fake_convert)

    def render(markdown: str) -> str:
        return recorder(markdown, "/pinned/pandoc", 30.0)

    with pytest.raises(ValueError, match="did not settle by pass 3"):
        generator.build_replay_fixture(
            "prose",
            ["source"],
            render=render,
            recorder=recorder,
        )


def test_static_replay_converter_returns_exact_bytes_and_rejects_unknown_input(
    generator: ModuleType,
) -> None:
    input_sha = hashlib.sha256(b"source").hexdigest()
    fixture = {
        "conversions": {
            input_sha: base64.b85encode(b"rendered").decode("ascii"),
        }
    }
    converter = generator.StaticReplayConverter([fixture])

    assert converter("source", "/not-executed/pandoc", 1.0) == "rendered"
    assert converter.calls == [input_sha]
    with pytest.raises(AssertionError, match="no committed Pandoc output"):
        converter("unknown", "/not-executed/pandoc", 1.0)


def test_non_writing_check_detects_drift_without_replacing_committed_bytes(
    generator: ModuleType,
    tmp_path: Path,
) -> None:
    fixture = {
        "schema_version": 1,
        "stratum": "prose",
        "conversion_count": 0,
        "conversions": {},
        "bodies": [],
    }
    output = tmp_path / "replay"
    generator.publish_replay_fixtures([fixture], output, check=False)
    committed = (output / "prose.json").read_bytes()

    generator.publish_replay_fixtures([fixture], output, check=True)
    drifted = dict(fixture, conversion_count=1)
    with pytest.raises(ValueError, match="committed replay fixture is stale"):
        generator.publish_replay_fixtures([drifted], output, check=True)

    assert (output / "prose.json").read_bytes() == committed
