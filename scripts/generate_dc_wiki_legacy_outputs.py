#!/usr/bin/env python3
"""Regenerate deterministic DC wiki-renderer compatibility and replay outputs.

The legacy manifest is a historical compatibility oracle, while the per-stratum
replay artifacts drive broad routine tests without subprocesses. Regenerate them
only when intentionally establishing a reviewed byte-compatibility baseline, using
the pinned ``wiki`` extra:

    env PATH="$PWD/.venv/bin:$PATH" \
      python scripts/generate_dc_wiki_legacy_outputs.py

Without ``--check`` the command rewrites the legacy manifest and all three replay
files. ``--check`` is non-writing: it executes every corpus unit and required
settling pass through the installed product path, then compares the canonical bytes
with the committed replay files. This is the External Integration Tests entrypoint.

The historical conversion deliberately freezes the landed pre-hardening subprocess
contract instead of importing production conversion constants. Replay capture drives
the production segmenter and converter. No timestamps or machine-local paths enter
the replay JSON, so supported binaries emit byte-identical fixture bytes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import platform
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = REPO_ROOT / "src" / "rebar" / "_engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from rebar_reconciler.adapters.jira_family import wiki_render  # noqa: E402

DEFAULT_CORPUS = REPO_ROOT / "tests" / "fixtures" / "dc_wiki_corpus"
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "fixtures" / "dc_wiki_legacy_outputs.json"
DEFAULT_REPLAY_OUTPUT = REPO_ROOT / "tests" / "fixtures" / "dc_wiki_replay"

_STRATA = ("code_arrow", "table", "prose")
_EXPECTED_UNIT_COUNT = 884
_MAX_FIXTURE_BYTES = 500_000

# Frozen historical conversion contract. Do not replace these with
# ``wiki_render._PANDOC_*``: independence from those mutable values is the point
# of the committed oracle.
LEGACY_PANDOC_FROM = "commonmark"
LEGACY_PANDOC_TO = "jira"
LEGACY_PANDOC_ARGS = ("--wrap=none",)
LEGACY_PANDOC_VERSION = "3.9"
SUPPORTED_PANDOC_BINARY_SHA256 = {
    "darwin-arm64": "d7b1e75cd20ee6a788a1399492be07d4559949e18e55f34cfc5c91807fdfa90d",
    "linux-x86_64": "decd3dd11a3fe0c16ce56443343ec53adde6fbed6f97d7f56f06b1c424248e7b",
}
_LEGACY_ANCHOR_RE = re.compile(r"\{anchor:[^}]*\}")

Runner = Callable[..., subprocess.CompletedProcess[str]]
Converter = Callable[[str, str], str | None]
ReplayConverter = Callable[[str, str, float | None], str | None]
Renderer = Callable[[str], str]
BinaryHasher = Callable[[Path], str]


def stable_platform_key(system: str, machine: str) -> str:
    """Return the stable OS/architecture key used by Pandoc provenance."""
    normalized_system = system.strip().lower()
    normalized_machine = machine.strip().lower()
    architecture = {
        "aarch64": "arm64",
        "amd64": "x86_64",
        "x64": "x86_64",
    }.get(normalized_machine, normalized_machine)
    return f"{normalized_system}-{architecture}"


def current_platform_key() -> str:
    """Identify this host without using Python-version-dependent platform text."""
    return stable_platform_key(platform.system(), platform.machine())


def validate_pandoc_provenance(
    provenance: object,
    *,
    platform_key: str,
    version: str,
    binary_sha256: str,
) -> None:
    """Validate one installed Pandoc against the complete frozen provenance contract."""
    if not isinstance(provenance, dict):
        raise ValueError("Pandoc provenance must be a JSON object")

    expected_version = provenance.get("version")
    if expected_version != LEGACY_PANDOC_VERSION:
        raise ValueError(
            "Pandoc provenance version is invalid: "
            f"expected {LEGACY_PANDOC_VERSION!r}, found {expected_version!r}"
        )

    fingerprints = provenance.get("supported_platform_binary_sha256")
    if not isinstance(fingerprints, dict):
        raise ValueError("Pandoc provenance must contain a supported-platform fingerprint map")
    expected_platforms = set(SUPPORTED_PANDOC_BINARY_SHA256)
    actual_platforms = set(fingerprints)
    if actual_platforms != expected_platforms:
        missing = sorted(expected_platforms - actual_platforms)
        unexpected = sorted(actual_platforms - expected_platforms)
        raise ValueError(
            "Pandoc supported-platform fingerprint map is incomplete: "
            f"missing={missing}, unexpected={unexpected}"
        )
    if fingerprints != SUPPORTED_PANDOC_BINARY_SHA256:
        raise ValueError(
            "Pandoc supported-platform fingerprint map differs from the frozen contract"
        )

    generating_platform = provenance.get("generating_platform")
    generating_hash = provenance.get("binary_sha256")
    if generating_platform not in fingerprints:
        raise ValueError(
            f"Pandoc generating platform {generating_platform!r} is not supported; "
            f"supported platforms: {sorted(fingerprints)}"
        )
    if generating_hash != fingerprints[generating_platform]:
        raise ValueError(
            f"Pandoc generating binary SHA-256 mismatch for {generating_platform}: "
            f"expected {fingerprints[generating_platform]}, found {generating_hash}"
        )

    if version != expected_version:
        raise ValueError(
            f"Pandoc version mismatch for {platform_key}: "
            f"expected {expected_version}, installed {version}"
        )
    if platform_key not in fingerprints:
        raise ValueError(
            f"unsupported Pandoc platform {platform_key!r}; "
            f"supported platforms: {sorted(fingerprints)}"
        )
    expected_hash = fingerprints[platform_key]
    if binary_sha256 != expected_hash:
        raise ValueError(
            f"Pandoc binary SHA-256 mismatch for {platform_key}: "
            f"expected {expected_hash}, installed {binary_sha256}"
        )


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 hex digest used by fixture identities."""
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """Fingerprint a file without loading the Pandoc executable wholly in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def legacy_convert(
    markdown: str,
    pandoc: str,
    *,
    runner: Runner = subprocess.run,
) -> str | None:
    """Run the landed pre-hardening conversion contract over one prepared unit."""
    try:
        completed = runner(
            [
                pandoc,
                "-f",
                LEGACY_PANDOC_FROM,
                "-t",
                LEGACY_PANDOC_TO,
                *LEGACY_PANDOC_ARGS,
            ],
            input=markdown,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return _LEGACY_ANCHOR_RE.sub("", completed.stdout).rstrip("\n")


def renderable_units(corpus: Path = DEFAULT_CORPUS) -> list[str]:
    """Return every Pandoc-bound corpus unit in its authoritative order."""
    bodies: list[str] = []
    for stratum in _STRATA:
        payload = json.loads((corpus / f"{stratum}.json").read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(isinstance(body, str) for body in payload):
            raise ValueError(f"{stratum}.json must contain a JSON list of strings")
        bodies.extend(payload)

    units: list[str] = []
    for body in bodies:
        units.extend(
            wiki_render.substitute_arrows(text)
            for kind, text in wiki_render._lock_and_split(body)
            if kind == "render"
        )
    return units


def build_fixture(
    units: Iterable[str],
    *,
    pandoc: str,
    pandoc_version: str,
    generating_platform: str,
    converter: Converter = legacy_convert,
    binary_hasher: BinaryHasher = sha256_file,
) -> dict[str, Any]:
    """Convert ordered units and return the deterministic fixture payload."""
    binary_sha256 = binary_hasher(Path(pandoc))
    pandoc_provenance = {
        "version": pandoc_version,
        "generating_platform": generating_platform,
        "binary_sha256": binary_sha256,
        "supported_platform_binary_sha256": dict(SUPPORTED_PANDOC_BINARY_SHA256),
    }
    validate_pandoc_provenance(
        pandoc_provenance,
        platform_key=generating_platform,
        version=pandoc_version,
        binary_sha256=binary_sha256,
    )

    entries: list[dict[str, str | None]] = []
    for prepared in units:
        output = converter(prepared, pandoc)
        if output is not None and not isinstance(output, str):
            raise TypeError("converter must return str or None")
        entries.append(
            {
                "input_sha256": sha256_bytes(prepared.encode("utf-8")),
                # Base85 keeps exact UTF-8 bytes while preventing captured prose
                # from being mistaken for authored source by repository vocabulary
                # scanners. It is also materially smaller than Base64, preserving
                # headroom below the added-file limit.
                "output_b85": (
                    None
                    if output is None
                    else base64.b85encode(output.encode("utf-8")).decode("ascii")
                ),
            }
        )

    payload = {
        "schema_version": 3,
        "generator": "scripts/generate_dc_wiki_legacy_outputs.py",
        "pandoc": pandoc_provenance,
        "conversion": {
            "from": LEGACY_PANDOC_FROM,
            "to": LEGACY_PANDOC_TO,
            "args": list(LEGACY_PANDOC_ARGS),
            "postprocess": "strip-pandoc-anchor-macros-and-trailing-newlines",
        },
        "unit_count": len(entries),
        "output_encoding": "utf-8/base85",
        "replay": {
            "schema_version": 1,
            "directory": "tests/fixtures/dc_wiki_replay",
            "strata": list(_STRATA),
            "verify_mode": "committed-static-outputs-plus-three-live-bodies",
            "complete_live_mode": "external-integration-weekly-and-manual",
        },
        "units": entries,
    }
    return payload


def serialize_fixture(payload: dict[str, Any]) -> bytes:
    """Encode fixture JSON canonically and compactly, with one terminal newline."""
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _encode_output(value: str | None) -> str | None:
    if value is None:
        return None
    return base64.b85encode(value.encode("utf-8")).decode("ascii")


def _decode_output(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("replay output must be a base85 string or null")
    try:
        return base64.b85decode(value.encode("ascii")).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("replay output is not valid UTF-8/base85") from exc


class ReplayRecorder:
    """Record every real conversion while preserving product-call semantics."""

    def __init__(self, converter: ReplayConverter) -> None:
        self._converter = converter
        self.calls: list[str] = []
        self.conversions: dict[str, str | None] = {}

    def __call__(
        self,
        markdown: str,
        pandoc: str,
        timeout: float | None = None,
    ) -> str | None:
        output = self._converter(markdown, pandoc, timeout)
        if output is not None and not isinstance(output, str):
            raise TypeError("converter must return str or None")
        input_sha256 = sha256_bytes(markdown.encode("utf-8"))
        encoded = _encode_output(output)
        previous = self.conversions.get(input_sha256, encoded)
        if previous != encoded:
            raise ValueError(
                f"Pandoc produced different bytes for the same prepared input: {input_sha256}"
            )
        self.calls.append(input_sha256)
        self.conversions[input_sha256] = encoded
        return output


class StaticReplayConverter:
    """Test-only converter backed exclusively by committed replay outputs."""

    def __init__(self, fixtures: Iterable[dict[str, Any]]) -> None:
        self._outputs: dict[str, str | None] = {}
        self.calls: list[str] = []
        for fixture in fixtures:
            conversions = fixture.get("conversions")
            if not isinstance(conversions, dict):
                raise ValueError("replay fixture conversions must be a JSON object")
            for input_sha256, encoded in conversions.items():
                if not isinstance(input_sha256, str):
                    raise ValueError("replay conversion keys must be SHA-256 strings")
                output = _decode_output(encoded)
                if input_sha256 in self._outputs and self._outputs[input_sha256] != output:
                    raise ValueError(f"replay fixtures disagree for prepared input {input_sha256}")
                self._outputs[input_sha256] = output

    def __call__(
        self,
        markdown: str,
        _pandoc: str,
        _timeout: float | None = None,
    ) -> str | None:
        input_sha256 = sha256_bytes(markdown.encode("utf-8"))
        self.calls.append(input_sha256)
        if input_sha256 not in self._outputs:
            raise AssertionError(
                "no committed Pandoc output for prepared input "
                f"{input_sha256}; regenerate the replay fixtures"
            )
        return self._outputs[input_sha256]


def build_replay_fixture(
    stratum: str,
    bodies: Sequence[str],
    *,
    render: Renderer,
    recorder: ReplayRecorder,
) -> dict[str, Any]:
    """Capture every conversion and body output through the required settling pass."""
    if stratum not in _STRATA:
        raise ValueError(f"unknown corpus stratum: {stratum}")

    captured_bodies: list[dict[str, Any]] = []
    for body in bodies:
        first = render(body)
        second = render(first)
        pass_outputs = [first, second]
        if second != first:
            third = render(second)
            pass_outputs.append(third)
            if third != second:
                raise ValueError(
                    f"{stratum} body {sha256_bytes(body.encode('utf-8'))} did not settle by pass 3"
                )
        captured_bodies.append(
            {
                "source_sha256": sha256_bytes(body.encode("utf-8")),
                "pass_output_sha256": [
                    sha256_bytes(output.encode("utf-8")) for output in pass_outputs
                ],
            }
        )

    return {
        "schema_version": 1,
        "generator": "scripts/generate_dc_wiki_legacy_outputs.py",
        "stratum": stratum,
        "pandoc": {
            "version": LEGACY_PANDOC_VERSION,
            "supported_platform_binary_sha256": dict(SUPPORTED_PANDOC_BINARY_SHA256),
        },
        "output_encoding": "utf-8/base85",
        "body_count": len(captured_bodies),
        "conversion_count": len(recorder.calls),
        "conversion_trace": list(recorder.calls),
        "conversions": dict(recorder.conversions),
        "bodies": captured_bodies,
    }


def publish_replay_fixtures(
    fixtures: Iterable[dict[str, Any]],
    output: Path,
    *,
    check: bool,
) -> tuple[int, int]:
    """Write canonical fixtures, or compare them without mutating the checkout."""
    fixture_count = 0
    byte_count = 0
    for fixture in fixtures:
        stratum = fixture.get("stratum")
        if stratum not in _STRATA:
            raise ValueError(f"replay fixture has unknown stratum: {stratum!r}")
        encoded = serialize_fixture(fixture)
        if len(encoded) >= _MAX_FIXTURE_BYTES:
            raise ValueError(
                f"{stratum} replay fixture is {len(encoded)} bytes; "
                f"repository limit is {_MAX_FIXTURE_BYTES - 1}"
            )
        destination = output / f"{stratum}.json"
        if check:
            committed = destination.read_bytes() if destination.is_file() else None
            if committed != encoded:
                raise ValueError(
                    f"committed replay fixture is stale: {destination}; regenerate without --check"
                )
        else:
            output.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(encoded)
        fixture_count += 1
        byte_count += len(encoded)
    return fixture_count, byte_count


def load_replay_fixtures(output: Path = DEFAULT_REPLAY_OUTPUT) -> list[dict[str, Any]]:
    """Load the three committed replay fixtures in authoritative stratum order."""
    fixtures: list[dict[str, Any]] = []
    for stratum in _STRATA:
        path = output / f"{stratum}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"replay fixture must be a JSON object: {path}")
        if payload.get("schema_version") != 1 or payload.get("stratum") != stratum:
            raise ValueError(f"replay fixture identity is invalid: {path}")
        if payload.get("output_encoding") != "utf-8/base85":
            raise ValueError(f"replay fixture encoding is invalid: {path}")
        fixtures.append(payload)
    return fixtures


def _corpus_bodies(corpus: Path, stratum: str) -> list[str]:
    payload = json.loads((corpus / f"{stratum}.json").read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(body, str) for body in payload):
        raise ValueError(f"{stratum}.json must contain a JSON list of strings")
    return payload


def generate_replay(
    *,
    corpus: Path = DEFAULT_CORPUS,
    manifest: Path = DEFAULT_OUTPUT,
    output: Path = DEFAULT_REPLAY_OUTPUT,
    check: bool,
) -> tuple[int, int, int]:
    """Capture or validate every real conversion through corpus settling."""
    try:
        import pypandoc
    except ImportError as exc:  # pragma: no cover - exercised by direct invocation
        raise RuntimeError("install the pinned `wiki` extra before replaying") from exc

    pandoc = str(pypandoc.get_pandoc_path())
    pandoc_version = str(pypandoc.get_pandoc_version())
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest_payload, dict):
        raise ValueError("legacy manifest must be a JSON object")
    validate_pandoc_provenance(
        manifest_payload.get("pandoc"),
        platform_key=current_platform_key(),
        version=pandoc_version,
        binary_sha256=sha256_file(Path(pandoc)),
    )

    real_convert = wiki_render._convert
    fixtures: list[dict[str, Any]] = []
    for stratum in _STRATA:
        print(f"replaying {stratum} through real Pandoc...", flush=True)
        recorder = ReplayRecorder(real_convert)
        with (
            mock.patch.object(wiki_render, "_pandoc_path", return_value=pandoc),
            mock.patch.object(wiki_render, "_convert", recorder),
        ):
            fixture = build_replay_fixture(
                stratum,
                _corpus_bodies(corpus, stratum),
                render=wiki_render.render_markdown_to_wiki,
                recorder=recorder,
            )
        fixtures.append(fixture)
        print(
            f"captured {stratum}: {fixture['body_count']} bodies, "
            f"{fixture['conversion_count']} conversions",
            flush=True,
        )

    fixture_count, byte_count = publish_replay_fixtures(fixtures, output, check=check)
    conversion_count = sum(int(fixture["conversion_count"]) for fixture in fixtures)
    return fixture_count, conversion_count, byte_count


def generate(
    *,
    corpus: Path = DEFAULT_CORPUS,
    output: Path = DEFAULT_OUTPUT,
) -> tuple[int, int]:
    """Generate the fixture and return ``(unit_count, byte_count)``."""
    try:
        import pypandoc
    except ImportError as exc:  # pragma: no cover - exercised by direct invocation
        raise RuntimeError("install the pinned `wiki` extra before regenerating") from exc

    pandoc = str(pypandoc.get_pandoc_path())
    pandoc_version = str(pypandoc.get_pandoc_version())
    units = renderable_units(corpus)
    if len(units) != _EXPECTED_UNIT_COUNT:
        raise ValueError(
            f"expected {_EXPECTED_UNIT_COUNT} ordered renderable units, found {len(units)}"
        )

    payload = build_fixture(
        units,
        pandoc=pandoc,
        pandoc_version=pandoc_version,
        generating_platform=current_platform_key(),
    )
    encoded = serialize_fixture(payload)
    if len(encoded) >= _MAX_FIXTURE_BYTES:
        raise ValueError(
            f"fixture is {len(encoded)} bytes; repository limit is {_MAX_FIXTURE_BYTES - 1}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded)
    return len(units), len(encoded)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replay-output", type=Path, default=DEFAULT_REPLAY_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="execute complete real replay and compare committed bytes without writing",
    )
    args = parser.parse_args(argv)

    if not args.check:
        unit_count, legacy_bytes = generate(corpus=args.corpus, output=args.output)
        print(f"wrote {args.output}: {unit_count} units, {legacy_bytes} bytes")
    fixture_count, conversion_count, replay_bytes = generate_replay(
        corpus=args.corpus,
        manifest=args.output,
        output=args.replay_output,
        check=args.check,
    )
    verb = "validated" if args.check else "wrote"
    print(
        f"{verb} {fixture_count} replay fixtures in {args.replay_output}: "
        f"{conversion_count} real conversions, {replay_bytes} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
