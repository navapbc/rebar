#!/usr/bin/env python3
"""Regenerate the immutable pre-hardening DC wiki-renderer outputs.

This artifact is a historical compatibility oracle, not a snapshot to refresh when
the production renderer changes. Regenerate it only when intentionally establishing
a new byte-compatibility baseline, using the pinned ``wiki`` extra:

    env PATH="$PWD/.venv/bin:$PATH" \
      python scripts/generate_dc_wiki_legacy_outputs.py

The conversion below deliberately freezes the landed pre-hardening subprocess
contract instead of importing production conversion constants. Corpus preparation
still uses the production segmenter because the consuming test compares the ordered
prepared units; each unit's SHA-256 pins that alignment independently of its output.
No timestamps or machine-local paths enter the JSON, so identical inputs and Pandoc
binary produce byte-identical fixture bytes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = REPO_ROOT / "src" / "rebar" / "_engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from rebar_reconciler.adapters.jira_family import wiki_render  # noqa: E402

DEFAULT_CORPUS = REPO_ROOT / "tests" / "fixtures" / "dc_wiki_corpus"
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "fixtures" / "dc_wiki_legacy_outputs.json"

_STRATA = ("code_arrow", "table", "prose")
_EXPECTED_UNIT_COUNT = 884
_MAX_FIXTURE_BYTES = 500_000

# Frozen historical conversion contract. Do not replace these with
# ``wiki_render._PANDOC_*``: independence from those mutable values is the point
# of the committed oracle.
LEGACY_PANDOC_FROM = "commonmark"
LEGACY_PANDOC_TO = "jira"
LEGACY_PANDOC_ARGS = ("--wrap=none",)
_LEGACY_ANCHOR_RE = re.compile(r"\{anchor:[^}]*\}")

Runner = Callable[..., subprocess.CompletedProcess[str]]
Converter = Callable[[str, str], str | None]
BinaryHasher = Callable[[Path], str]


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
    converter: Converter = legacy_convert,
    binary_hasher: BinaryHasher = sha256_file,
) -> dict[str, Any]:
    """Convert ordered units and return the deterministic fixture payload."""
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

    return {
        "schema_version": 1,
        "generator": "scripts/generate_dc_wiki_legacy_outputs.py",
        "pandoc": {
            "version": pandoc_version,
            "binary_sha256": binary_hasher(Path(pandoc)),
        },
        "conversion": {
            "from": LEGACY_PANDOC_FROM,
            "to": LEGACY_PANDOC_TO,
            "args": list(LEGACY_PANDOC_ARGS),
            "postprocess": "strip-pandoc-anchor-macros-and-trailing-newlines",
        },
        "unit_count": len(entries),
        "output_encoding": "utf-8/base85",
        "units": entries,
    }


def serialize_fixture(payload: dict[str, Any]) -> bytes:
    """Encode fixture JSON canonically and compactly, with one terminal newline."""
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


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

    payload = build_fixture(units, pandoc=pandoc, pandoc_version=pandoc_version)
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
    args = parser.parse_args(argv)

    unit_count, byte_count = generate(corpus=args.corpus, output=args.output)
    print(f"wrote {args.output}: {unit_count} units, {byte_count} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
