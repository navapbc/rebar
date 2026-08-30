#!/usr/bin/env python3
"""Deploy-manifest completeness gate — derive-and-diff over autodeploy.sh's ``*_PATHS``.

``infra/scripts/autodeploy.sh`` maintains hand-curated ``*_PATHS`` manifests (BOT_PATHS,
SECRETS_PATHS, MCP_PATHS, CONFIG_PATHS, EDGE_PATHS, MATERIALIZER_PATHS, OBS_PATHS,
CERTBOT_PATHS, …) that decide which merged file changes trigger a redeploy / detect-signal.
When a deploy-relevant infra file is ADDED but never listed, a later change to it drifts to
the running environment silently. This exact class has recurred four times (nginx edge,
host-nginx materializers, mcp-entrypoint.sh, materialize-deploy-key.sh) — a manual list
falling out of sync with reality that no test caught.

This gate does NOT introduce a second curated "deploy-relevant files" list (that just
relocates the drift). Instead it DERIVES the expected path set from two sources already
maintained for their own reasons, and fails on drift — the same derive-and-diff shape as the
sibling ``scripts/check_server_manifest.py`` (which derives ``server.json`` from a code
inventory and rejects missing/extra/changed records):

1. **Dockerfile / compose directives** — ``COPY`` / ``install -m … <script>`` / ``ENTRYPOINT``
   script references under ``infra/compose/Dockerfile.*`` + ``docker-compose.yml`` (what is
   baked into the running images). Catches mcp-entrypoint.sh, named in Dockerfile.mcp.
2. **Filename conventions** — ``infra/**/materialize-*.sh``, ``infra/**/*-entrypoint.sh`` and
   ``infra/**/compose-up.sh``. Catches the materialize-*.sh drift class.

Each derived path is cross-referenced against the UNION of autodeploy.sh's ``*_PATHS`` using
the same git-pathspec prefix semantics autodeploy itself uses to decide "did a matching path
change". A derived path covered by NO manifest exits non-zero, printing the path and which
derivation matched it.

**Fail-safe exclusion list.** ``EXCLUSIONS`` records derived paths that are genuinely,
intentionally NOT deploy-triggering; each carries an inline reason. autodeploy.sh already
documents one such deliberate exclusion (there is intentionally NO ``AUTODEPLOY_PATHS`` block
— a self-update re-exec race, with a multi-line WHY near the manifests), so the guard MUST be
able to encode "derived but intentionally not deploy-triggering". The guard therefore
OVER-flags (add the path, or annotate the exclusion) and never silently misses — the opposite
failure mode from a bare hand-list.

Runs in-process via ``make lint`` (``python scripts/check_deploy_manifest.py``), mirroring the
~two-dozen sibling ``scripts/check_*.py`` guards: portable to diverse-CI / no-CI environments,
enforced by the pre-commit hook and any CI that runs ``make lint``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# Fail-safe exclusion list — derived paths intentionally NOT deploy-triggering.
# Each entry MUST carry a non-empty reason and name a real repo file, else this
# gate flags the exclusion itself as stale. To silence a genuine false positive,
# add the path here WITH a reason; to fix a real omission, list it in autodeploy.
# --------------------------------------------------------------------------- #
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
    tokens: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^[A-Z_]+_PATHS=(.*)$", line)
        if not match:
            continue
        rhs = match.group(1).strip()
        if rhs and rhs[0] in "'\"":
            rhs = rhs[1:]
        if rhs and rhs[-1] in "'\"":
            rhs = rhs[:-1]
        for token in rhs.split():
            token = token.strip("'\"")
            if token:
                tokens.add(token)
    return tokens


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


def check(repo_root: Path = REPO_ROOT) -> list[str]:
    """Return diagnostics (empty == clean) for the deploy-manifest completeness gate."""
    diagnostics: list[str] = []
    for path, reason in sorted(EXCLUSIONS.items()):
        if not reason.strip():
            diagnostics.append(f"EXCLUSION {path} carries no reason")
        if not (repo_root / path).is_file():
            diagnostics.append(f"EXCLUSION {path} names no repo file (stale — remove or fix it)")

    derived = derive_paths(repo_root)
    # Liveness floor: a silently-empty derivation (an infra restructure, a Dockerfile renamed
    # off the `Dockerfile.*` glob, a moved scripts dir) would let this gate pass VACUOUSLY —
    # exactly the fail-open class it exists to prevent. Refuse a zero-path derivation.
    if not derived:
        diagnostics.append(
            "DERIVATION EMPTY — no deploy-relevant paths were derived from any source. The "
            "Dockerfile/compose parser or the infra/ layout likely changed; refusing to pass "
            "vacuously (a silent empty derivation is the fail-open this gate prevents)."
        )

    tokens = parse_manifest_paths(_autodeploy_path(repo_root).read_text())
    for path, sources in uncovered(derived, tokens, EXCLUSIONS):
        diagnostics.append(
            f"UNCOVERED {path} — derived from [{', '.join(sources)}] but listed in NO "
            f"autodeploy.sh *_PATHS manifest"
        )
    return diagnostics


def main(argv: list[str] | None = None) -> int:
    diagnostics = check(REPO_ROOT)
    if diagnostics:
        print("::error::deploy-manifest gate: derived deploy-relevant paths not covered")
        for diagnostic in diagnostics:
            print(f"  {diagnostic}")
        print("  Fix: add the path to the appropriate *_PATHS in infra/scripts/autodeploy.sh,")
        print("  or record it in EXCLUSIONS (with a reason) in this script if it is")
        print("  intentionally not deploy-triggering.")
        return 1

    covered = len(derive_paths(REPO_ROOT))
    print(
        f"deploy-manifest completeness: OK. {covered} derived deploy-relevant paths all "
        f"covered by autodeploy.sh *_PATHS."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
