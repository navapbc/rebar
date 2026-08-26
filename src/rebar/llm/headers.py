"""Operator-configured request headers for gate LLM calls: value grammar + safety.

A standalone leaf module (imports only :mod:`rebar.llm.errors` and the stdlib) so the
grammar is testable without building an :class:`~rebar.llm.config.LLMConfig`, and so
``config.py`` — already near the 800-line module cap — gains only a field and one call.

**This module is env-PURE: it never touches ``os.environ``.** Ambient configuration is
read only at an approved composition root (RP-04 S7.1, enforced by
``scripts/check_config_ownership.py``, which classifies seams by BASENAME — ``config.py``
is one, this file is not). ``LLMConfig.from_env`` therefore does the ``REBAR_LLM_HEADERS``
read and threads the environment in as an explicit ``Mapping``. That is also what makes
the ``${env:...}`` grammar testable against a plain dict, with no process environment
in play.

Two things live here, and both are CLOSED sets owned by this module rather than derived
from whatever the caller happens to supply:

* the ``${...}`` namespaces (:data:`NAMESPACES`) and the ``${run:...}`` key vocabulary
  (:data:`RUN_KEYS`) — so a typo fails at CONFIGURATION time, not mid-gate-run;
* the denied header names (:data:`DENIED_HEADER_NAMES`).

**Every failure is loud.** Unlike ``mcp_servers`` — which degrades malformed JSON to
``{}`` in all three layers (``config.py``) — a bad header configuration raises
:class:`~rebar.llm.errors.LLMConfigError` naming the offending layer. Silently discarding
headers would leave an operator with an unattributed gateway and no signal.

**Ordering is part of the contract: substitute FIRST, then validate.** Every check runs
on the RESOLVED value. Validating the literal instead would let an env var whose value
holds CR/LF smuggle an injected header past the check meant to stop it: a configured
``${env:GATEWAY_TAG}`` where ``GATEWAY_TAG`` is ``foo\\r\\nInjected: bar`` passes a check
applied to the literal, and substitution then injects. ``${run:...}`` placeholders are
still present as literal text during validation (they hold no control characters); the
delivery ticket re-runs validation after substituting those.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from rebar.llm.errors import LLMConfigError

# ── the closed grammar ────────────────────────────────────────────────────────────────
ENV_NAMESPACE = "env"
RUN_NAMESPACE = "run"
#: The ONLY recognized ``${<namespace>:...}`` namespaces. Anything else is a hard error.
NAMESPACES = frozenset({ENV_NAMESPACE, RUN_NAMESPACE})
#: The ONLY recognized ``${run:KEY}`` keys. Their VALUES exist only inside a gate run, so
#: the placeholder survives config resolution unresolved — but the key is checked now.
RUN_KEYS = frozenset({"trace_id", "ticket_id", "operation"})

# ── the safety denylist ───────────────────────────────────────────────────────────────
#: Header names an operator may NOT set. Both vendor SDKs merge caller headers LAST, so a
#: caller-supplied ``Authorization`` would silently displace the real credential; the rest
#: are protocol-routing headers. A denylist rather than an allowlist because arbitrary
#: gateway header names are the whole point of this surface, while the threats closed —
#: credential displacement and protocol injection — are themselves closed sets. Matched
#: case-insensitively but by EXACT name, so ``x-authorization-context`` is accepted.
DENIED_HEADER_NAMES = frozenset(
    {"authorization", "x-api-key", "cookie", "host", "proxy-authorization"}
)
#: The supported channel a denied name is redirected to. Named in the rejection message so
#: the denylist redirects to a first-class field rather than merely forbidding.
_CREDENTIAL_FIELD = "llm.api_key"

# RFC 7230 token characters — the complete set a header field-name may use.
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
# Request-splitting / truncation characters. Never legal in a header field-value.
_FORBIDDEN_VALUE_CHARS = {"\r": "CR", "\n": "LF", "\0": "NUL"}


def _resolve_placeholder(body: str, *, name: str, env: Mapping[str, str]) -> str:
    """Dispose ONE ``${...}`` body (the text between the braces). Returns the substituted
    text for ``env``, or the placeholder verbatim for a valid ``run`` key."""
    namespace, colon, key = body.partition(":")
    if not colon:
        raise LLMConfigError(
            f"llm.headers[{name!r}]: placeholder '${{{body}}}' has no namespace separator; "
            f"expected '${{<namespace>:<key>}}' with namespace one of "
            f"{sorted(NAMESPACES)}"
        )
    if namespace not in NAMESPACES:
        raise LLMConfigError(
            f"llm.headers[{name!r}]: unrecognized placeholder namespace {namespace!r} in "
            f"'${{{body}}}'; expected one of {sorted(NAMESPACES)}"
        )
    if namespace == RUN_NAMESPACE:
        if key not in RUN_KEYS:
            raise LLMConfigError(
                f"llm.headers[{name!r}]: unknown ${{run:...}} key {key!r}; expected one of "
                f"{sorted(RUN_KEYS)}"
            )
        # Left UNRESOLVED on purpose: its value exists only inside a gate run. The key was
        # checked above, so a typo fails here at configuration time rather than at call time.
        return f"${{{RUN_NAMESPACE}:{key}}}"
    value = env.get(key)
    if value is None:
        raise LLMConfigError(
            f"llm.headers[{name!r}]: '${{env:{key}}}' names environment variable {key!r}, "
            f"which is not set; a missing value is never silently degraded"
        )
    return value


def substitute(value: str, *, name: str, env: Mapping[str, str]) -> str:
    """Resolve the ``${...}`` grammar in one header value.

    ``$$`` escapes to a literal ``$`` ANYWHERE in the value — the OpenTelemetry Collector
    rule, adopted whole so ``$${VAR}`` yields ``${VAR}`` unsubstituted and ``a$$b`` yields
    ``a$b``. Otherwise any ``${`` begins a placeholder, and an unclosed one is a hard error
    rather than a silently-accepted literal (passing placeholder text through as a header
    value would be a silent misconfiguration).
    """
    out: list[str] = []
    i, end_of_value = 0, len(value)
    while i < end_of_value:
        if value.startswith("$$", i):
            out.append("$")
            i += 2
            continue
        if not value.startswith("${", i):
            out.append(value[i])
            i += 1
            continue
        close = value.find("}", i + 2)
        if close < 0:
            raise LLMConfigError(
                f"llm.headers[{name!r}]: unclosed placeholder in {value!r}; "
                f"'${{' must be closed by '}}' (write '$$' for a literal '$')"
            )
        out.append(_resolve_placeholder(value[i + 2 : close], name=name, env=env))
        i = close + 1
    return "".join(out)


def validate_header(name: str, value: str) -> None:
    """Reject an unsafe header. ``value`` MUST already be substituted — see the module
    docstring's ordering contract."""
    if name.lower() in DENIED_HEADER_NAMES:
        raise LLMConfigError(
            f"llm.headers: header {name!r} may not be set; both vendor SDKs merge these "
            f"headers last, so it would displace the configured credential. Use "
            f"{_CREDENTIAL_FIELD} instead."
        )
    if not _TOKEN_RE.match(name):
        raise LLMConfigError(
            f"llm.headers: header name {name!r} contains characters outside the RFC 7230 token set"
        )
    for char, label in _FORBIDDEN_VALUE_CHARS.items():
        if char in value:
            raise LLMConfigError(
                f"llm.headers[{name!r}]: resolved value contains {label}, which would allow "
                f"header injection"
            )


def resolve_header_map(raw: dict, *, layer: str, env: Mapping[str, str]) -> dict[str, str]:
    """Substitute then validate every entry of one already-parsed header mapping."""
    resolved: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise LLMConfigError(
                f"{layer}: header entries must be string name/value pairs; got "
                f"{type(name).__name__} -> {type(value).__name__}"
            )
        substituted = substitute(value, name=name, env=env)
        validate_header(name, substituted)
        resolved[name] = substituted
    return resolved


def _from_json(raw: str, *, layer: str, env: Mapping[str, str]) -> dict[str, str]:
    """Parse ONE layer's JSON object of headers, then resolve it. Malformed JSON and a
    non-object document are both hard errors naming ``layer``."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMConfigError(f"{layer}: malformed JSON ({exc})") from exc
    if not isinstance(parsed, dict):
        raise LLMConfigError(
            f"{layer}: expected a JSON object of headers, got {type(parsed).__name__}"
        )
    return resolve_header_map(parsed, layer=layer, env=env)


def _cli_raw(cli: dict):
    """The ``rebar -c llm.headers=<json>`` override for this resolution.

    ``cli`` is the section mapping :func:`rebar.config.cli_overrides_for` returns for
    ``llm`` — the nested shape :func:`rebar.config.parse_cli_overrides` builds from the
    ``-c`` flag, already narrowed to this section. Nothing else is consulted:
    ``reset_config_cache()`` deliberately clears the process-wide CLI overrides (its own
    docstring says so), so reaching around it to resurrect them would let a
    reserved-section module override a documented core semantic.
    """
    return cli.get("headers")


def resolve_headers(
    table: dict, cli: dict, *, env_json: str | None, env: Mapping[str, str]
) -> dict[str, str]:
    """The three-layer resolution behind ``LLMConfig.headers``.

    Precedence mirrors ``mcp_servers``: ``rebar -c llm.headers=<json>`` > the
    ``REBAR_LLM_HEADERS`` env JSON > the ``[tool.rebar.llm].headers`` file value (a native
    TOML table, or a JSON string). The PRECEDENCE is copied; the error handling is NOT —
    see the module docstring.

    ``env_json`` is the already-read ``REBAR_LLM_HEADERS`` value (``None`` when unset) and
    ``env`` is the mapping ``${env:VAR}`` resolves against. BOTH are supplied by the caller:
    the ambient read belongs to the composition root, not here (see the module docstring).
    """
    cli_raw = _cli_raw(cli)
    if cli_raw is not None and str(cli_raw).strip():
        return _from_json(str(cli_raw), layer="rebar -c llm.headers", env=env)
    if env_json is not None and env_json.strip():
        return _from_json(env_json, layer="REBAR_LLM_HEADERS", env=env)
    file_value = table.get("headers")
    if file_value is None:
        return {}
    if isinstance(file_value, dict):
        return resolve_header_map(file_value, layer="[tool.rebar.llm].headers", env=env)
    if isinstance(file_value, str):
        return _from_json(file_value, layer="[tool.rebar.llm].headers", env=env)
    raise LLMConfigError(
        f"[tool.rebar.llm].headers: expected a table or a JSON string, got "
        f"{type(file_value).__name__}"
    )


# The ``${run:KEY}`` placeholders left behind by :func:`substitute`. Only the key is captured;
# the key vocabulary is re-checked against :data:`RUN_KEYS` so a placeholder that reached this
# seam without passing config resolution (a hand-built mapping, a future caller) still fails
# loudly rather than being delivered verbatim.
_RUN_PLACEHOLDER_RE = re.compile(r"\$\{" + RUN_NAMESPACE + r":([^}]*)\}")


def resolve_run_headers(
    headers: Mapping[str, str], *, run: Mapping[str, str | None]
) -> dict[str, str]:
    """The finished headers for ONE call: ``${run:...}`` resolved against this run's identity.

    Returns a FRESH dict every call, never ``headers`` itself. That copy is load-bearing, not
    hygiene: at the pinned pydantic-ai the Chat Completions path reads
    ``model_settings.get('extra_headers', {})`` with no copy and then ``setdefault``s a
    ``User-Agent`` onto it (upstream issue 6866, fixed by PR 6868 after this pin), so handing
    over the config's own mapping would pollute it across calls and leak a version string into
    the recorded header-name set.

    ``run`` maps each :data:`RUN_KEYS` name to this run's value, or ``None`` where there is no
    such value. Three dispositions, deliberately different:

    * an UNKNOWN key raises :class:`~rebar.llm.errors.LLMConfigError` naming it — the same
      config-time error, restated here because this seam accepts mappings that never passed
      through :func:`resolve_headers`;
    * an ABSENT substituend (``None`` or empty) OMITS the whole header. Only the two gate
      boundaries mint an identity, so ``review-code`` and every standalone op legitimately have
      no ticket or trace; an empty correlation header pollutes the gateway and raising would
      break those ops merely because headers are configured globally. Observability must never
      fail an operation. Sibling headers with no placeholder are unaffected;
    * a PRESENT substituend is substituted, and the finished header is then RE-VALIDATED. A
      value that was a clean placeholder at config time is not proven safe until the run value
      has landed in it, so :func:`validate_header` runs again on the resolved text — the same
      substitute-then-validate ordering the module docstring states, applied to this second seam.
    """
    resolved: dict[str, str] = {}
    for name, value in headers.items():
        substituted = _substitute_run(value, name=name, run=run)
        if substituted is None:
            continue  # an absent substituend drops the header — see the docstring
        validate_header(name, substituted)
        resolved[name] = substituted
    return resolved


def _substitute_run(value: str, *, name: str, run: Mapping[str, str | None]) -> str | None:
    """One header value with its ``${run:...}`` placeholders replaced, or ``None`` when any
    substituend is absent (the caller drops such a header entirely)."""
    absent = False

    def _one(match: re.Match[str]) -> str:
        nonlocal absent
        key = match.group(1)
        if key not in RUN_KEYS:
            raise LLMConfigError(
                f"llm.headers[{name!r}]: unknown ${{run:...}} key {key!r}; expected one of "
                f"{sorted(RUN_KEYS)}"
            )
        run_value = run.get(key)
        if not run_value:
            absent = True
            return ""
        return run_value

    substituted = _RUN_PLACEHOLDER_RE.sub(_one, value)
    return None if absent else substituted
