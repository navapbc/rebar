"""Confirm package existence through manifests and deps.dev without asserting absence.

The T0 lane returns refuted only for a confirmed registry hit; internal, stdlib,
ambiguous, unknown, missing, and transient cases abstain with closed reasons.
Names follow each ecosystem's registry rules. Enumeration uses stdlib parsers;
optional syft is not required. _http_get is the sole network seam."""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import evidence as ev

# ── Ecosystem vocabulary ─────────────────────────────────────────────────────

#: The purl ecosystems this lane understands, mapped to the deps.dev `system`.
#: The reference's ``ecosystem`` field uses the purl spelling (left); deps.dev
#: uses its own (right). ``golang`` → ``go`` is the notable rename.
_DEPSDEV_SYSTEM: dict[str, str] = {
    "pypi": "pypi",
    "npm": "npm",
    "cargo": "cargo",
    "golang": "go",
    "go": "go",
    "maven": "maven",
    "nuget": "nuget",
}

#: Ecosystems we can *enumerate* and *normalize* but for which deps.dev has no
#: existence endpoint — probing abstains (unsupported), enumeration still works.
_NO_REGISTRY_ORACLE: frozenset[str] = frozenset({"gem"})

_BACKEND = "registry"
_DEPSDEV_BASE = "https://api.deps.dev/v3/systems"
_USER_AGENT = "rebar-grounding/T0 (+https://deps.dev existence probe)"
_HTTP_TIMEOUT = 10.0

# Node.js builtin modules (no scheme prefix). A `require('fs')` is not an npm dep.
_NODE_BUILTINS: frozenset[str] = frozenset(
    {
        "assert",
        "async_hooks",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "constants",
        "crypto",
        "dgram",
        "diagnostics_channel",
        "dns",
        "domain",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "inspector",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "sys",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
    }
)

# Go standard-library top-level paths (no domain ⇒ no dot in the first segment is
# the heuristic; this set covers the common explicit cases).
_GO_STD_FIRST: frozenset[str] = frozenset(
    {
        "fmt",
        "errors",
        "strings",
        "strconv",
        "bytes",
        "io",
        "os",
        "net",
        "time",
        "sort",
        "sync",
        "context",
        "math",
        "encoding",
        "crypto",
        "bufio",
        "regexp",
        "unicode",
        "reflect",
        "runtime",
        "container",
        "hash",
        "log",
        "path",
        "testing",
        "flag",
        "bufbuild",
        "embed",
        "slices",
        "maps",
        "cmp",
    }
)

#: A small, high-confidence import→distribution map for the mismatch gauntlet.
#: Presence here means "this import name is NOT the distribution name" → abstain
#: (we cannot decide existence at T0 without the distribution name).
_IMPORT_DIST_MISMATCH: dict[str, str] = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "yaml": "pyyaml",
    "pil": "pillow",
    "sklearn": "scikit-learn",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "google": "google-api-python-client",
    "jose": "python-jose",
    "win32api": "pywin32",
}


# ── Name normalization (per ecosystem) ───────────────────────────────────────


def normalize_name(ecosystem: str, name: str) -> str:
    """Return a registry-normalized name: PEP 503 for PyPI, case-insensitive
    Cargo and npm (preserving npm scopes), Go uppercase !-escaping, or a trimmed
    unchanged name for other ecosystems."""
    n = name.strip()
    eco = ecosystem.lower()
    if eco == "pypi":
        # PEP 503: lowercase and collapse [-_.] runs; trim edge separators to match registry names.
        return re.sub(r"[-_.]+", "-", n).strip("-").lower()
    if eco == "cargo":
        return n.lower()
    if eco == "npm":
        return n.lower()
    if eco in ("go", "golang"):
        return _go_escape(n)
    return n


def _go_escape(module_path: str) -> str:
    """Apply Go module-path case-escaping (uppercase ``X`` → ``!x``).

    Go module paths are case-sensitive; the proxy/deps.dev convention encodes an
    uppercase letter as ``!`` + its lowercase form so the path is filesystem-safe.
    """
    out: list[str] = []
    for ch in module_path:
        if ch.isupper():
            out.append("!" + ch.lower())
        else:
            out.append(ch)
    return "".join(out)


# ── The abstain gauntlet (pre-probe guards) ──────────────────────────────────


def _stdlib_abstain(eco: str, name: str) -> dict[str, Any] | None:
    """Return an abstain record iff ``name`` is a stdlib/builtin (not a dist)."""
    base = name.split("/")[-1] if eco == "npm" and name.startswith("@") else name
    is_std = False
    if eco == "pypi":
        # Some stdlib names also exist on PyPI; abstain rather than infer a distribution.
        is_std = base.split(".")[0] in sys.stdlib_module_names
    elif eco == "npm":
        is_std = base in _NODE_BUILTINS
    elif eco in ("go", "golang"):
        first = name.split("/")[0]
        # A std import path has no dot in its first segment (no domain) AND is a
        # known std root — both guards so a vanity host isn't mistaken for std.
        is_std = "." not in first and first in _GO_STD_FIRST
    if is_std:
        return ev.abstain(
            ev.DEFAULT_REASON,
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend=_BACKEND,
            reference=_dep_reference(name, eco),
            detail="stdlib — not a distribution; a registry 404 here would NOT mean hallucinated",
        )
    return None


def _import_mismatch_abstain(eco: str, name: str) -> dict[str, Any] | None:
    """Abstain iff ``name`` is a known import alias differing from its dist name."""
    if eco != "pypi":
        return None
    if name.lower() in _IMPORT_DIST_MISMATCH:
        dist = _IMPORT_DIST_MISMATCH[name.lower()]
        return ev.abstain(
            "ambiguous",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend=_BACKEND,
            reference=_dep_reference(name, eco),
            detail=f"import name {name!r} differs from distribution name (likely {dist!r}); "
            "cannot decide existence at T0 without the distribution name",
        )
    return None


def _workspace_abstain(
    eco: str, name: str, workspace_members: set[str] | None
) -> dict[str, Any] | None:
    """Abstain iff ``name`` is a local workspace/monorepo member (internal)."""
    if not workspace_members:
        return None
    norm = normalize_name(eco, name)
    members = {normalize_name(eco, m) for m in workspace_members}
    if norm in members or name in workspace_members:
        return ev.abstain(
            "private_or_internal_suspected",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend=_BACKEND,
            reference=_dep_reference(name, eco),
            detail="declared as a local workspace/monorepo member — internal, not a public-registry package",  # noqa: E501
        )
    return None


def _dep_reference(name: str, ecosystem: str) -> dict[str, Any]:
    return {"kind": "dependency", "name": name, "ecosystem": ecosystem}


# ── Registry probe (deps.dev) ────────────────────────────────────────────────


def _http_get(url: str, *, timeout: float = _HTTP_TIMEOUT) -> int:
    """Return the HTTP status for url, including error statuses.

    This is the sole network seam. Transport errors propagate for the caller's
    fail-open classification."""
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _probe_registry(eco: str, name: str) -> dict[str, Any]:
    """Probe deps.dev and return evidence without raising.

    200 refutes absence; 404/410 abstain as possibly private or internal. 429,
    5xx, timeout, and other transport errors abstain with closed reasons."""
    system = _DEPSDEV_SYSTEM[eco]
    quoted = urllib.parse.quote(name, safe="")
    url = f"{_DEPSDEV_BASE}/{system}/packages/{quoted}"
    cov_ran = ev.coverage(backend=_BACKEND, status=ev.STATUS_RAN)
    ref = _dep_reference(name, eco)
    try:
        code = _http_get(url)
    except (
        TimeoutError
    ) as exc:  # urllib raises TimeoutError (or socket.timeout subclass) on read timeout
        return ev.abstain(
            "timeout",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend=_BACKEND,
            reference=ref,
            detail=f"registry probe timed out: {exc!r}",
        )
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            return ev.abstain(
                "timeout",
                job=ev.JOB_REFUTE,
                provenance_tier=ev.TIER_T0,
                backend=_BACKEND,
                reference=ref,
                detail=f"registry probe timed out: {exc!r}",
            )
        return ev.abstain(
            "network_error",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend=_BACKEND,
            reference=ref,
            detail=f"registry unreachable: {exc!r}",
        )
    except OSError as exc:  # DNS/connection-level
        return ev.abstain(
            "network_error",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend=_BACKEND,
            reference=ref,
            detail=f"registry probe failed: {exc!r}",
        )

    if code == 200:
        return ev.refuted(
            provenance_tier=ev.TIER_T0,
            coverage=cov_ran,
            reference=ref,
            detail=f"exists on deps.dev ({system}) — the 'does-not-exist' claim is false",
        )
    if code in (404, 410):
        return ev.abstain(
            "private_or_internal_suspected",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend=_BACKEND,
            reference=ref,
            detail=f"not on public registry (HTTP {code}) — cannot prove absence; "
            "could be private/internal/new. NEVER 'absent'.",
        )
    if code == 429:
        return ev.abstain(
            "rate_limited",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend=_BACKEND,
            reference=ref,
            detail=f"registry rate-limited (HTTP {code})",
        )
    if 500 <= code <= 599:
        return ev.abstain(
            "network_error",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend=_BACKEND,
            reference=ref,
            detail=f"registry server error (HTTP {code})",
        )
    # Any other status (e.g. 401/403/3xx) — undecidable; abstain rather than guess.
    return ev.abstain(
        "other",
        job=ev.JOB_REFUTE,
        provenance_tier=ev.TIER_T0,
        backend=_BACKEND,
        reference=ref,
        detail=f"unexpected registry status (HTTP {code})",
    )


# ── Public API: refute_package ───────────────────────────────────────────────


def refute_package(
    reference: dict[str, Any],
    *,
    workspace_members: set[str] | None = None,
) -> dict[str, Any]:
    """Refute an absent-dependency claim only when registry existence is confirmed.

    The structured reference names an ecosystem and package. Workspace members,
    stdlib names, import aliases, unknown ecosystems, not-found results, and probe
    failures abstain; every record follows the evidence schema."""
    name = str(reference.get("name", "")).strip()
    eco = str(reference.get("ecosystem", "")).strip().lower()

    if not name:
        return ev.abstain(
            "ambiguous",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend=_BACKEND,
            detail="empty package name",
        )

    # Unknown / unsupported ecosystem → abstain, never absent.
    if eco not in _DEPSDEV_SYSTEM and eco not in _NO_REGISTRY_ORACLE:
        return ev.abstain(
            "unsupported_lang",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend=_BACKEND,
            reference=_dep_reference(name, eco or "unknown"),
            detail=f"unknown/unsupported ecosystem {eco or '<empty>'!r}",
        )

    # Gauntlet — each guard short-circuits to an abstain BEFORE any network call.
    for guard in (
        _stdlib_abstain(eco, name),
        _workspace_abstain(eco, name, workspace_members),
        _import_mismatch_abstain(eco, name),
    ):
        if guard is not None:
            return guard

    # Ecosystem we can normalize/enumerate but have no existence oracle for.
    if eco in _NO_REGISTRY_ORACLE:
        return ev.abstain(
            "unsupported_lang",
            job=ev.JOB_REFUTE,
            provenance_tier=ev.TIER_T0,
            backend=_BACKEND,
            reference=_dep_reference(name, eco),
            detail=f"no public existence oracle wired for ecosystem {eco!r} (enumeration only)",
        )

    normalized = normalize_name(eco, name)
    rec = _probe_registry(eco, normalized)
    # Preserve the ORIGINAL asserted name on the reference (we probed normalized).
    if isinstance(rec.get("reference"), dict):
        rec["reference"]["name"] = name
        if normalized != name:
            extra = f"normalized to {normalized!r}"
            rec["detail"] = f"{rec.get('detail', '')} [{extra}]".strip()
    return rec


def refute_packages(
    references: list[dict[str, Any]],
    *,
    workspace_members: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Refute a batch of references — one record each (polyglot-safe)."""
    return [refute_package(r, workspace_members=workspace_members) for r in references]


# ── Manifest / lockfile enumeration ──────────────────────────────────────────

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:[<>=!~;@\[].*)?$")


def enumerate_dependencies(root: str | Path) -> dict[str, Any]:
    """Enumerate and deduplicate manifest dependencies by ecosystem and name.

    Returns references, normalized workspace members, and per-file errors across
    Python, npm, Cargo, Go, Maven, and Gem manifests. Unreadable or invalid files
    are skipped with errors; optional syft is not invoked."""
    base = Path(root)
    refs: dict[tuple[str, str], dict[str, Any]] = {}
    members: set[str] = set()
    errors: list[dict[str, str]] = []

    def add(eco: str, name: str) -> None:
        name = name.strip()
        if name:
            refs.setdefault((eco, name), _dep_reference(name, eco))

    parsers = (
        ("pyproject.toml", _parse_pyproject),
        ("requirements.txt", _parse_requirements),
        ("package.json", _parse_package_json),
        ("Cargo.toml", _parse_cargo_toml),
        ("go.mod", _parse_go_mod),
        ("pom.xml", _parse_pom_xml),
        ("Gemfile", _parse_gemfile),
    )
    for filename, parser in parsers:
        path = base / filename
        if not path.is_file():
            continue
        try:
            deps, mem = parser(path)
        except Exception as exc:  # noqa: BLE001 — per-manifest fail-open: the error is recorded in-band (errors) and the manifest skipped, never aborting enumeration
            errors.append({"file": filename, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        for eco, name in deps:
            add(eco, name)
        members.update(mem)

    # Also pick up extra requirements*.txt variants.
    for extra in base.glob("requirements*.txt"):
        if extra.name == "requirements.txt":
            continue
        try:
            deps, _ = _parse_requirements(extra)
        except Exception as exc:  # noqa: BLE001 — per-manifest fail-open: the error is recorded in-band (errors) and the file skipped, never aborting enumeration
            errors.append({"file": extra.name, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        for eco, name in deps:
            add(eco, name)

    return {
        "references": list(refs.values()),
        "workspace_members": members,
        "errors": errors,
    }


# Each parser returns (list[(ecosystem, name)], set[workspace_member_name]).


def _parse_pyproject(path: Path) -> tuple[list[tuple[str, str]], set[str]]:
    import tomllib

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    proj = data.get("project", {})
    for spec in proj.get("dependencies", []) or []:
        name = _pep508_name(str(spec))
        if name:
            out.append(("pypi", name))
    for group in (proj.get("optional-dependencies", {}) or {}).values():
        for spec in group or []:
            name = _pep508_name(str(spec))
            if name:
                out.append(("pypi", name))
    # Poetry layout.
    poetry = data.get("tool", {}).get("poetry", {})
    for key in ("dependencies", "dev-dependencies"):
        for dep_name in poetry.get(key, {}) or {}:
            if dep_name.lower() != "python":
                out.append(("pypi", dep_name))
    return out, set()


def _pep508_name(spec: str) -> str:
    spec = spec.strip()
    m = _REQ_LINE.match(spec)
    return m.group(1) if m else ""


def _parse_requirements(path: Path) -> tuple[list[tuple[str, str]], set[str]]:
    out: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):  # skip blanks, comments, -r/-e flags
            continue
        name = _pep508_name(line)
        if name:
            out.append(("pypi", name))
    return out, set()


def _parse_package_json(path: Path) -> tuple[list[tuple[str, str]], set[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for name in data.get(field, {}) or {}:
            out.append(("npm", name))
    members: set[str] = set()
    # npm/yarn workspaces: {"workspaces": [...]} or {"workspaces": {"packages": [...]}}
    ws = data.get("workspaces")
    if isinstance(ws, dict):
        ws = ws.get("packages")
    if isinstance(ws, list):
        # Workspace globs reference local member dirs; their package names aren't
        # known without reading each — record the package's own name as a member.
        if data.get("name"):
            members.add(str(data["name"]))
    return out, members


def _parse_cargo_toml(path: Path) -> tuple[list[tuple[str, str]], set[str]]:
    import tomllib

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        for name, spec in (data.get(section, {}) or {}).items():
            # A path/workspace dep is local, not a crates.io package.
            if isinstance(spec, dict) and ("path" in spec or spec.get("workspace")):
                continue
            out.append(("cargo", name))
    members: set[str] = set()
    workspace = data.get("workspace", {})
    if isinstance(workspace, dict):
        for member_path in workspace.get("members", []) or []:
            # The directory's basename is the conventional crate name.
            members.add(Path(str(member_path)).name)
    if "package" in data and isinstance(data["package"], dict):
        pkg_name = data["package"].get("name")
        if pkg_name:
            members.add(str(pkg_name))
    return out, members


def _parse_go_mod(path: Path) -> tuple[list[tuple[str, str]], set[str]]:
    out: list[tuple[str, str]] = []
    replaced: set[str] = set()
    in_require = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("require ("):
            in_require = True
            continue
        if in_require and line == ")":
            in_require = False
            continue
        if line.startswith("require "):
            mod = line[len("require ") :].strip().split()
            if mod:
                out.append(("golang", mod[0]))
            continue
        if in_require:
            parts = line.split()
            if parts:
                out.append(("golang", parts[0]))
            continue
        if line.startswith("replace "):
            # `replace X => ./local` marks X as locally-provided (internal).
            body = line[len("replace ") :]
            lhs = body.split("=>", 1)[0].strip().split()
            if lhs:
                replaced.add(lhs[0])
    return out, replaced


def _parse_pom_xml(path: Path) -> tuple[list[tuple[str, str]], set[str]]:
    import xml.etree.ElementTree as ET

    text = path.read_text(encoding="utf-8")
    # Strip the default namespace so tag matching is simple and robust.
    text = re.sub(r'\sxmlns="[^"]+"', "", text, count=1)
    root = ET.fromstring(text)
    out: list[tuple[str, str]] = []
    for dep in root.iter("dependency"):
        gid = dep.findtext("groupId")
        aid = dep.findtext("artifactId")
        if gid and aid:
            out.append(("maven", f"{gid.strip()}:{aid.strip()}"))
    members: set[str] = set()
    for mod in root.iter("module"):
        if mod.text:
            members.add(mod.text.strip())
    return out, members


def _parse_gemfile(path: Path) -> tuple[list[tuple[str, str]], set[str]]:
    out: list[tuple[str, str]] = []
    pat = re.compile(r"""^\s*gem\s+['"]([^'"]+)['"]""")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0]
        m = pat.match(line)
        if m:
            out.append(("gem", m.group(1)))
    return out, set()
