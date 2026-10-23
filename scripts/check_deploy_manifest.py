#!/usr/bin/env python3
"""Check that derived deployment paths are covered by autodeploy manifests.

Derive paths from Dockerfile/Compose COPY, install, and ENTRYPOINT references
plus materialize, entrypoint, compose-up, and cap script filename patterns.
Compare them with the union of ``autodeploy.sh`` ``*_PATHS`` using its git
pathspec prefix rules. Report any uncovered path and its derivation. Explicit
``EXCLUSIONS`` need reasons and are checked for stale entries; an empty
derived set fails rather than passing vacuously. Runs through ``make lint``."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

# Intentional non-triggering derived paths need a reason and a live file;
# stale exclusions fail. Add true omissions to autodeploy instead.
EXCLUSIONS: dict[str, str] = {
    "infra/scripts/install-autodeploy.sh": (
        "autodeploy's OWN installer / self-update is DELIBERATELY excluded from in-run "
        "re-materialization — there is intentionally no AUTODEPLOY_PATHS block. Re-materializing "
        "it in-run would make the running rebar-autodeploy unit rewrite + daemon-reload its own "
        "service/timer mid-execution (re-exec race), and would re-assert units behind the "
        "operator's deliberate staged-rollout gate. Owned by the provisioning/operator layer, "
        "not the unattended re-materializer. See the multi-line WHY comment in autodeploy.sh."
    ),
}

# Directory the image build contexts copy in as `/app` (`COPY . /app`), so a baked-in
# `/app/<repo-relative>` script maps back to `<repo-relative>` in the checkout.
_BUILD_CONTEXT_PREFIX = "/app/"


def _autodeploy_path(repo_root: Path) -> Path:
    return repo_root / "infra" / "scripts" / "autodeploy.sh"


def _compose_dir(repo_root: Path) -> Path:
    return repo_root / "infra" / "compose"


def parse_manifest_paths(text: str) -> set[str]:
    """Return the UNION of every path token across all ``^[A-Z_]+_PATHS=`` assignments."""
    return {token for tokens in parse_manifest_paths_by_name(text).values() for token in tokens}


def parse_manifest_paths_by_name(text: str) -> dict[str, set[str]]:
    """Return path tokens keyed by their ``*_PATHS`` manifest assignment name."""
    manifests: dict[str, set[str]] = {}
    tokens: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^([A-Z_]+_PATHS)=(.*)$", line)
        if not match:
            continue
        name = match.group(1)
        rhs = match.group(2).strip()
        if rhs and rhs[0] in "'\"":
            # Read only the quoted value: a trailing shell comment must not become a
            # fake manifest path and mask an uncovered deployment file.
            quote = rhs[0]
            close = rhs.find(quote, 1)
            rhs = rhs[1:close] if close != -1 else rhs[1:]
        else:
            # Unquoted value: a shell inline comment starts at an ` #`.
            rhs = rhs.split(" #", 1)[0]
        for token in rhs.split():
            token = token.strip("'\"")
            if token:
                tokens.add(token)
        manifests[name] = tokens
        tokens = set()
    return manifests


def is_covered(path: str, tokens: set[str]) -> bool:
    """True when ``path`` is a manifest token or lives under one (git-pathspec semantics)."""
    for token in tokens:
        base = token.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


def _resolve_repo_script(raw: str, repo_root: Path) -> str | None:
    """Map a Dockerfile/compose script reference to an existing repo-relative path."""
    if not raw.endswith(".sh"):
        return None
    if raw.startswith(_BUILD_CONTEXT_PREFIX):
        candidate = raw[len(_BUILD_CONTEXT_PREFIX) :]
        return candidate if (repo_root / candidate).is_file() else None
    if raw.startswith("/"):
        # An installed/container-absolute path (e.g. an ENTRYPOINT). Resolve by basename
        # against the checkout; a generated-in-image script (no repo source) resolves to
        # nothing and is correctly skipped.
        matches = sorted(
            str(p.relative_to(repo_root))
            for p in repo_root.glob("infra/**/" + Path(raw).name)
            if p.is_file()
        )
        return matches[0] if len(matches) == 1 else None
    return raw if (repo_root / raw).is_file() else None


def _iter_sh_tokens(value: str) -> list[str]:
    """Extract ``.sh`` tokens from a directive value (JSON-array or shell form)."""
    return [tok.strip("[](),'\"") for tok in value.split() if ".sh" in tok]


def _derive_from_dockerfile(path: Path, repo_root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("COPY "):
            source, label = stripped.split(None, 1)[1], "COPY"
            tokens = [t for t in source.split() if t.endswith(".sh")]
        elif upper.startswith("ENTRYPOINT") or upper.startswith("CMD"):
            tokens, label = _iter_sh_tokens(stripped), "ENTRYPOINT"
        elif "install -m" in stripped:
            tokens, label = _iter_sh_tokens(stripped), "install"
        else:
            continue
        for raw in tokens:
            resolved = _resolve_repo_script(raw, repo_root)
            if resolved:
                found.append((resolved, f"{path.name}:{label}"))
    return found


def _derive_from_compose(path: Path, repo_root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not re.match(r"^(entrypoint|command):", stripped):
            continue
        for raw in _iter_sh_tokens(stripped.split(":", 1)[1]):
            resolved = _resolve_repo_script(raw, repo_root)
            if resolved:
                found.append((resolved, f"{path.name}:{stripped.split(':', 1)[0]}"))
    return found


def _derive_from_conventions(repo_root: Path) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    patterns = (
        "infra/**/materialize-*.sh",
        "infra/**/*-entrypoint.sh",
        "infra/**/compose-up.sh",
        "infra/**/*-cap.sh",
    )
    for pattern in patterns:
        label = "glob:" + pattern.split("/")[-1]
        for match in repo_root.glob(pattern):
            if match.is_file():
                found.append((str(match.relative_to(repo_root)), label))
    return found


def derive_paths(repo_root: Path = REPO_ROOT) -> dict[str, list[str]]:
    """Derive deploy-relevant infra paths, mapping each to the sources that named it."""
    found: list[tuple[str, str]] = []
    compose_dir = _compose_dir(repo_root)
    for dockerfile in sorted(compose_dir.glob("Dockerfile.*")):
        found += _derive_from_dockerfile(dockerfile, repo_root)
    compose = compose_dir / "docker-compose.yml"
    if compose.is_file():
        found += _derive_from_compose(compose, repo_root)
    found += _derive_from_conventions(repo_root)

    derived: dict[str, set[str]] = {}
    for path, source in found:
        derived.setdefault(path, set()).add(source)
    return {path: sorted(sources) for path, sources in derived.items()}


def uncovered(
    derived: dict[str, list[str]], tokens: set[str], exclusions: dict[str, str]
) -> list[tuple[str, list[str]]]:
    """Derived paths in NO manifest token and not intentionally excluded."""
    return [
        (path, sources)
        for path, sources in sorted(derived.items())
        if path not in exclusions and not is_covered(path, tokens)
    ]


_CAP_MANIFESTS = ("MATERIALIZER_PATHS", "OBS_PATHS")


def _cap_manifest_gaps(
    derived: dict[str, list[str]], manifests: dict[str, set[str]], exclusions: dict[str, str]
) -> list[tuple[str, str, list[str]]]:
    caps = {
        path: sources
        for path, sources in derived.items()
        if path.endswith("-cap.sh") and path not in exclusions
    }
    return [
        (path, manifest, sources)
        for path, sources in sorted(caps.items())
        for manifest in _CAP_MANIFESTS
        if not is_covered(path, manifests.get(manifest, set()))
    ]


def check_text(manifest_text: str, repo_root: Path = REPO_ROOT) -> list[str]:
    """Return diagnostics (empty == clean) for the deploy-manifest completeness gate."""
    diagnostics: list[str] = []
    for path, reason in sorted(EXCLUSIONS.items()):
        if not reason.strip():
            diagnostics.append(f"EXCLUSION {path} carries no reason")
        if not (repo_root / path).is_file():
            diagnostics.append(f"EXCLUSION {path} names no repo file (stale — remove or fix it)")

    derived = derive_paths(repo_root)
    # A zero-path derivation would pass vacuously after an infra layout
    # change; reject it as a liveness failure.
    if not derived:
        diagnostics.append(
            "DERIVATION EMPTY — no deploy-relevant paths were derived from any source. The "
            "Dockerfile/compose parser or the infra/ layout likely changed; refusing to pass "
            "vacuously (a silent empty derivation is the fail-open this gate prevents)."
        )

    manifests = parse_manifest_paths_by_name(manifest_text)
    tokens = {token for paths in manifests.values() for token in paths}
    for path, sources in uncovered(derived, tokens, EXCLUSIONS):
        diagnostics.append(
            f"UNCOVERED {path} — derived from [{', '.join(sources)}] but listed in NO "
            f"autodeploy.sh *_PATHS manifest"
        )
    for path, manifest, sources in _cap_manifest_gaps(derived, manifests, EXCLUSIONS):
        diagnostics.append(
            f"UNCOVERED {path} — derived from [{', '.join(sources)}] but listed in NO "
            f"{manifest} consuming manifest"
        )
    return diagnostics


def check(repo_root: Path = REPO_ROOT) -> list[str]:
    """Return diagnostics (empty == clean) for the deploy-manifest completeness gate."""
    return check_text(_autodeploy_path(repo_root).read_text(), repo_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="deploy-manifest completeness gate")
    parser.add_argument("--root", default=str(REPO_ROOT), help="repo root to check")
    args = parser.parse_args(argv)
    repo_root = Path(args.root)

    diagnostics = check(repo_root)
    if diagnostics:
        print("::error::deploy-manifest gate: derived deploy-relevant paths not covered")
        for diagnostic in diagnostics:
            print(f"  {diagnostic}")
        print("  Fix: add the path to the appropriate *_PATHS in infra/scripts/autodeploy.sh,")
        print("  or record it in EXCLUSIONS (with a reason) in this script if it is")
        print("  intentionally not deploy-triggering.")
        return 1

    covered = len(derive_paths(repo_root))
    print(
        f"deploy-manifest completeness: OK. {covered} derived deploy-relevant paths all "
        f"covered by autodeploy.sh *_PATHS."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
