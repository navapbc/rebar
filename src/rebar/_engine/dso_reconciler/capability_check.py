#!/usr/bin/env python3
from __future__ import annotations

import os

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StepResult:
    name: str
    ok: bool
    message: str
    details: dict = field(default_factory=dict)


def run(repo_root: Path | None = None) -> StepResult:
    """Check that identity-layer bootstrap attestation files exist.

    Populates StepResult.details with the file-examination trail so operators
    can distinguish three failure modes that previously collapsed into one
    generic message: (a) no bootstrap dir at all, (b) dir exists but is empty,
    (c) files exist but are all malformed / missing the required 'band' key.
    """
    if repo_root is None:
        repo_root = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])  # project root

    bootstrap_dir = repo_root / "bridge_state" / "bootstrap"

    # Examination trail — empty on the bootstrap_dir-missing path so callers
    # can tell "no dir" from "dir with malformed files" by inspecting it.
    examined: list[str] = []
    parse_failures: list[dict] = []
    files_without_band: list[str] = []

    if not bootstrap_dir.is_dir():
        return StepResult(
            name="capability_check",
            ok=False,
            message=(
                "identity-layer attestation absent — bootstrap_dir does not "
                f"exist at {bootstrap_dir} — run cfd6 bootstrap first"
            ),
            details={"bootstrap_dir": str(bootstrap_dir), "exists": False},
        )

    attested_files = list(bootstrap_dir.glob("*.attested.json"))
    if not attested_files:
        return StepResult(
            name="capability_check",
            ok=False,
            message=(
                "identity-layer attestation absent — no *.attested.json files "
                f"in {bootstrap_dir} — run cfd6 bootstrap first"
            ),
            details={"bootstrap_dir": str(bootstrap_dir), "files_examined": []},
        )

    # Validate at least one attestation file has required 'band' key.
    # Track every examination so failures carry diagnostic context instead
    # of silently swallowing JSON-decode / IO errors.
    for attest_file in attested_files:
        examined.append(attest_file.name)
        try:
            data = json.loads(attest_file.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError covers binary-garbage attestations on hosts
            # with a non-UTF-8 default locale (e.g., LANG=C containers). These
            # land in parse_failures alongside JSON-shape and IO errors so the
            # diagnostic trail captures bytes-level malformedness too.
            entry: dict = {"file": attest_file.name, "error_type": type(exc).__name__}
            # str(exc) for JSONDecodeError can include context characters from
            # the offending content, which may leak attestation payload (band
            # secret, signing material) into telemetry sinks. For OSError and
            # UnicodeDecodeError, str(exc) carries operationally-load-bearing
            # information (errno + path; byte offset + codec) that does NOT
            # echo file contents, so we preserve it there.
            if not isinstance(exc, json.JSONDecodeError):
                entry["error"] = str(exc)
            parse_failures.append(entry)
            continue
        # Require 'band' to be a non-empty string AFTER stripping whitespace —
        # `'band' in data` would accept `{"band": null}` / `{"band": ""}` /
        # `{"band": []}`, and `data["band"]` alone would accept whitespace-only
        # strings like `{"band": "   "}` as truthy. Both gate downstream
        # pre_cutover on a meaningless value.
        band_value = data.get("band") if isinstance(data, dict) else None
        if isinstance(band_value, str) and band_value.strip():
            return StepResult(
                name="capability_check",
                ok=True,
                message=f"attestation found: {attest_file.name}",
                details={"files_examined": examined, "matched": attest_file.name},
            )
        # File parsed but lacked a usable 'band' key (missing, null, empty,
        # whitespace-only, or non-string).
        files_without_band.append(attest_file.name)

    return StepResult(
        name="capability_check",
        ok=False,
        message=(
            "identity-layer attestation absent — examined "
            f"{len(examined)} file(s), {len(parse_failures)} parse failure(s), "
            f"{len(files_without_band)} without 'band' key — run cfd6 bootstrap first"
        ),
        details={
            "bootstrap_dir": str(bootstrap_dir),
            "files_examined": examined,
            "parse_failures": parse_failures,
            "files_without_band": files_without_band,
        },
    )
