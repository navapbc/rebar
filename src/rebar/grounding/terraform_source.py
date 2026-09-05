"""Positive-only Terraform source availability probing (REB-640).

``probe_source`` disproves an asserted unavailability only when a declared local
module is inside the frozen snapshot or registry metadata is positively reachable.
Every access failure abstains with a closed reason; no branch downloads module
content, follows redirects, runs helpers, prompts, or exposes credentials.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import ssl
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import evidence as ev
from . import terraform_index as tfi
from . import terraform_receipt as tr

DEFAULT_REGISTRY = "registry.terraform.io"
MAX_METADATA_BYTES = 1 << 20
PROBE_REASON_DETAILS = tr.PROBE_REASON_DETAILS

# Reserved / private-use top-level names (RFC 2606 / RFC 6762 / common intranet TLDs):
# a source whose host ends in one of these is a PRIVATE target and needs a credential.
_PRIVATE_TLDS = frozenset(
    {"corp", "home", "internal", "intranet", "lan", "local", "localdomain", "private"}
)

_OPERATION = "probe_source"
_LANGUAGE = "terraform"
_TIMEOUT_SECONDS = 60
_SOURCE_KINDS = {"provider": "registry_provider", "module": "registry_module"}


@dataclass(frozen=True)
class HttpResult:
    status: int
    final_url: str
    body: bytes


class OversizedMetadata(Exception):
    """Raised when registry metadata exceeds the bounded read limit."""


class RejectedTarget(Exception):
    """Raised when a hostname RESOLVES to a private/reserved address (SSRF guard).

    Name-based classification (:func:`_is_private_host`) cannot catch a public-looking
    hostname that resolves to an internal IP (e.g. cloud-metadata ``169.254.169.254``),
    so the real probe resolves the host and refuses any non-global address before it
    connects. Unit tests mock :func:`_https_probe`, so this never issues live DNS there.
    """


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@dataclass(frozen=True)
class _RegistryTarget:
    kind: str
    source_kind: str
    host: str
    namespace: str
    name: str
    system: str | None = None


def _https_probe(url: str, *, headers: dict[str, str], timeout: int, max_bytes: int) -> HttpResult:
    """Fetch one HTTPS metadata URL without redirects and with a capped body."""
    _reject_private_resolution(url)
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as resp:
            body = _read_capped(resp, max_bytes)
            return HttpResult(status=resp.status, final_url=resp.geturl(), body=body)
    except urllib.error.HTTPError as exc:
        body = _read_capped(exc, max_bytes)
        return HttpResult(status=exc.code, final_url=url, body=body)


def probe(*, session: Any, source: str, from_module: str = "") -> Any:
    """Probe a Terraform module/provider source through ``session`` helpers."""
    query = {"operation": _OPERATION, "source": source, "from_module": from_module}
    module_dir = _normalize_from_module(session, from_module)
    if session._pending is not None:
        return session._abstain(_OPERATION, query, session._pending, module_dir or ".")
    if module_dir is None:
        return session._abstain(_OPERATION, query, "rejected_target", ".")
    normalized = unicodedata.normalize("NFC", source.strip())
    if _is_local_source(normalized):
        return _probe_local(session, query, normalized, module_dir)
    target, detail = _registry_target(normalized)
    if target is None:
        return session._abstain(_OPERATION, query, detail or "rejected_target", module_dir or ".")
    return _probe_registry(session, query, normalized, module_dir, target)


def _read_capped(fp: Any, max_bytes: int) -> bytes:
    body = fp.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise OversizedMetadata("Terraform registry metadata exceeded the read limit")
    return body


def _reject_private_resolution(url: str) -> None:
    """Raise :class:`RejectedTarget` if ``url``'s host resolves to a non-global address."""
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return  # a resolution failure is surfaced as dns_error by the connect attempt
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise RejectedTarget(host)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _normalize_from_module(session: Any, from_module: str) -> str | None:
    raw = (from_module or "").strip()
    if not raw:
        return "."
    try:
        rel = tfi._norm_rel(session._root, raw)
    except tfi.TerraformPathError:
        return None
    return "." if rel in ("", ".") else rel


def _is_local_source(source: str) -> bool:
    return source.startswith((".", "./", "..", "../"))


def _probe_local(session: Any, query: dict[str, Any], source: str, from_dir: str) -> Any:
    child_dir = tfi._resolve_child_dir(session._root, from_dir, source)
    if child_dir is None:
        return session._abstain(_OPERATION, query, "rejected_target", from_dir)
    if child_dir not in session._snapshot.modules:
        return session._abstain(_OPERATION, query, "dynamic_source", from_dir)
    if not session._has_terraform([child_dir]):
        return session._abstain(_OPERATION, query, "dynamic_source", from_dir)
    facts, detail = session._parse_module(child_dir)
    if detail is not None:
        return session._abstain(_OPERATION, query, detail, child_dir)
    location = _local_location(session, child_dir, facts)
    reference = {"kind": "source", "name": source, "language": _LANGUAGE}
    return session._refuted(
        _OPERATION,
        query,
        reference,
        location,
        child_dir,
        tier=ev.TIER_T1,
        source_kind="local_module",
        detail="auth_source=none",
    )


def _local_location(session: Any, module_dir: str, facts: dict[str, Any]) -> dict[str, Any]:
    declarations = facts.get("declarations", [])
    if declarations:
        return _location(declarations[0])
    files = session._module_files(module_dir)
    file = files[0] if files else module_dir
    return {"file": file, "line_start": 1, "line_end": 1}


def _location(decl: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": decl["file"],
        "line_start": decl["line_start"],
        "line_end": decl["line_end"],
    }


def _registry_target(source: str) -> tuple[_RegistryTarget | None, str | None]:
    if "${" in source:
        return None, "dynamic_source"
    if _has_embedded_credentials(source) or source.startswith("["):
        return None, "rejected_target"
    scheme_detail = _scheme_detail(source)
    if scheme_detail is not None:
        return None, scheme_detail
    parts = source.split("/")
    if any(not part or "\\" in part for part in parts):
        return None, "rejected_target"
    explicit_host = "." in parts[0] and parts[0] != ".."
    host = parts[0].lower() if explicit_host else DEFAULT_REGISTRY
    rest = parts[1:] if explicit_host else parts
    if not _valid_idents(rest) or not _valid_host(host):
        return None, "rejected_target"
    if len(rest) == 2:
        return _target("provider", host, rest[0], rest[1]), None
    if len(rest) == 3:
        return _target("module", host, rest[0], rest[1], rest[2]), None
    return None, "rejected_target"


def _has_embedded_credentials(source: str) -> bool:
    authority = source.split("/", 1)[0]
    if "://" in source:
        authority = source.split("://", 1)[1].split("/", 1)[0]
    return "@" in authority


def _scheme_detail(source: str) -> str | None:
    scheme_match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", source)
    if source.startswith("http://"):
        return "rejected_target"
    if "::" in source or scheme_match is not None:
        return "non_registry_remote"
    return None


def _valid_idents(parts: list[str]) -> bool:
    ident = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    return bool(parts) and all(ident.fullmatch(part) for part in parts)


def _target(
    kind: str, host: str, namespace: str, name: str, system: str | None = None
) -> _RegistryTarget:
    return _RegistryTarget(
        kind=kind,
        source_kind=_SOURCE_KINDS[kind],
        host=host,
        namespace=namespace,
        name=name,
        system=system,
    )


def _is_private_host(host: str) -> bool:
    """True iff ``host`` names a reserved/private-use target (needs a credential)."""
    return host.rsplit(".", 1)[-1].lower() in _PRIVATE_TLDS


def _valid_host(host: str) -> bool:
    if "@" in host or ":" in host or not host or host.startswith(".") or host.endswith("."):
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2:
        return False
    return all(_valid_host_label(label) for label in labels)


def _valid_host_label(label: str) -> bool:
    return (
        0 < len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and re.fullmatch(r"[A-Za-z0-9-]+", label) is not None
    )


def _probe_registry(
    session: Any,
    query: dict[str, Any],
    source: str,
    module_dir: str,
    target: _RegistryTarget,
) -> Any:
    token, auth_source = _credential_for_host(target.host)
    if token is None and _is_private_host(target.host):
        return session._abstain(_OPERATION, query, "rejected_target", module_dir)
    endpoint = _endpoint(target)
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    result, detail = _safe_network_probe(endpoint, headers)
    evidence_detail = f"auth_source={auth_source}"
    if detail is not None:
        return session._abstain(_OPERATION, query, detail, module_dir, detail=evidence_detail)
    result_detail = _result_detail(result, target.kind)
    if result_detail is not None:
        return session._abstain(
            _OPERATION, query, result_detail, module_dir, detail=evidence_detail
        )
    reference = {"kind": "source", "name": source, "language": _LANGUAGE}
    return session._refuted(
        _OPERATION,
        query,
        reference,
        None,
        module_dir,
        tier=ev.TIER_T0,
        source_kind=target.source_kind,
        detail=evidence_detail,
    )


def _endpoint(target: _RegistryTarget) -> str:
    if target.kind == "provider":
        return f"https://{target.host}/v1/providers/{target.namespace}/{target.name}/versions"
    return (
        f"https://{target.host}/v1/modules/"
        f"{target.namespace}/{target.name}/{target.system}/versions"
    )


def _safe_network_probe(
    endpoint: str, headers: dict[str, str]
) -> tuple[HttpResult | None, str | None]:
    try:
        return (
            _https_probe(
                endpoint,
                headers=headers,
                timeout=_TIMEOUT_SECONDS,
                max_bytes=MAX_METADATA_BYTES,
            ),
            None,
        )
    except OversizedMetadata:
        return None, "oversized_metadata"
    except RejectedTarget:
        return None, "rejected_target"
    except socket.gaierror:
        return None, "dns_error"
    except ssl.SSLError:
        return None, "tls_error"
    except TimeoutError:
        return None, "probe_timeout"
    except urllib.error.URLError as exc:
        return None, _url_error_detail(exc)
    except OSError:
        return None, "dns_error"


def _url_error_detail(exc: urllib.error.URLError) -> str:
    """Classify a transport ``URLError``: a TLS/timeout reason keeps its specific closed
    detail; otherwise a configured HTTPS proxy attributes the failure to ``proxy_error``."""
    reason = getattr(exc, "reason", None)
    if isinstance(reason, ssl.SSLError):
        return "tls_error"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "probe_timeout"
    if "https" in urllib.request.getproxies():
        return "proxy_error"
    return "dns_error"


def _result_detail(result: HttpResult | None, kind: str) -> str | None:
    if result is None:
        return "dns_error"
    status = result.status
    if 300 <= status < 400:
        return "rejected_redirect"
    if status in (401, 403):
        return "registry_unauthorized"
    if status in (404, 410):
        return "registry_not_found"
    if status == 429:
        return "registry_rate_limited"
    if 500 <= status < 600:
        return "registry_server_error"
    if not 200 <= status < 300:
        return "dns_error"
    return _metadata_detail(result.body, kind)


def _metadata_detail(body: bytes, kind: str) -> str | None:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "malformed_metadata"
    if not isinstance(decoded, dict):
        return "malformed_metadata"
    if kind == "module":
        return None if isinstance(decoded.get("modules"), list) else "malformed_metadata"
    if isinstance(decoded.get("versions"), list):
        return None
    return "malformed_metadata"


def _credential_for_host(host: str) -> tuple[str | None, str]:
    env_name = "TF_TOKEN_" + host.replace("-", "__").replace(".", "_")
    env_token = os.environ.get(env_name)  # read-via: terraform-registry-credential
    if env_token:
        return env_token, "environment"
    file_token = _static_file_token(host)
    if file_token is not None:
        return file_token, "static-file"
    return None, "none"


def _static_file_token(host: str) -> str | None:
    for path in _credential_paths():
        token = _token_from_file(path, host)
        if token is not None:
            return token
    return None


def _credential_paths() -> list[Path]:
    paths: list[Path] = []
    configured = os.environ.get("TF_CLI_CONFIG_FILE")  # read-via: terraform-cli-config
    if configured:
        paths.append(Path(configured).expanduser())
    else:
        paths.append(Path.home() / ".terraformrc")
    paths.append(Path.home() / ".terraform.d" / "credentials.tfrc.json")
    return paths


def _token_from_file(path: Path, host: str) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if path.name.endswith(".json"):
        return _token_from_json(text, host)
    return _token_from_hcl(text, host)


def _token_from_json(text: str, host: str) -> str | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    creds = data.get("credentials") if isinstance(data, dict) else None
    entry = creds.get(host) if isinstance(creds, dict) else None
    token = entry.get("token") if isinstance(entry, dict) else None
    return token if isinstance(token, str) and token else None


def _token_from_hcl(text: str, host: str) -> str | None:
    block_re = re.compile(r'credentials\s+"([^"]+)"\s*\{(.*?)\}', re.DOTALL)
    token_re = re.compile(r'token\s*=\s*"([^"]+)"')
    for match in block_re.finditer(text):
        if match.group(1) != host:
            continue
        token = token_re.search(match.group(2))
        if token is not None and token.group(1):
            return token.group(1)
    return None
