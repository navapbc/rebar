"""mirror_guard — health + drift checks for the Gerrit→GitHub mirror lock.

Ticket a774 (epic b744). After the WS7 cutover, GitHub ``main`` is a read-only mirror that
only advances via Gerrit's replication deploy key, gated by the ``gerrit-mirror-lock-main``
ruleset. Two silent-failure modes can then let Gerrit and GitHub drift apart unnoticed:

1. **Replication failing** — Gerrit ``main`` advances but the push to GitHub is rejected/stuck,
   so GitHub ``main`` falls behind.
2. **Ruleset drift** — the lock is mutated or deleted out-of-band (e.g. via the GitHub UI),
   silently re-opening ``main`` to human pushes.

This module is the tested *logic* behind two watchers (see the ticket): a scheduled GitHub
Actions workflow (primary) and a box CloudWatch probe (secondary, replication only). It is
**stdlib-only** (rebar core's contract — no boto3/requests): pure verdict functions plus thin
``urllib`` I/O and a CLI.

Verdict schema (both checks): ``{"check": str, "healthy": bool, "reason": str, ...}``.
CLI exit codes: ``0`` all healthy · ``1`` unhealthy (divergence/drift) · ``2`` fetch/IO error.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

# --- The lock contract (must match infra/terraform-github/main.tf) ---------
GERRIT_BASE_URL = "https://rebar.solutions.navateam.com"
GITHUB_REPO = "navapbc/rebar"
LOCK_RULESET_NAME = "gerrit-mirror-lock-main"
REQUIRED_RULE_TYPES = frozenset({"update", "deletion", "non_fast_forward"})
REQUIRED_INCLUDE_REFS = ["refs/heads/main"]

_EXIT_HEALTHY = 0
_EXIT_UNHEALTHY = 1
_EXIT_ERROR = 2


# ===========================================================================
# Pure verdict functions (no I/O — fully unit-testable)
# ===========================================================================
def replication_verdict(gerrit_sha: str | None, github_sha: str | None) -> dict[str, Any]:
    """In sync iff both SHAs are present and equal.

    A transient divergence (replication runs ~15s behind a submit) is expected and is NOT
    treated specially here — the *alarm's* evaluation window (sustained divergence) absorbs
    transient lag; this function reports the instantaneous truth.
    """
    if not gerrit_sha or not github_sha:
        return {
            "check": "replication",
            "healthy": False,
            "reason": f"missing SHA (gerrit={gerrit_sha!r}, github={github_sha!r})",
            "gerrit_sha": gerrit_sha,
            "github_sha": github_sha,
        }
    in_sync = gerrit_sha == github_sha
    return {
        "check": "replication",
        "healthy": in_sync,
        "reason": "in sync" if in_sync else "GitHub main is behind/ahead of Gerrit main",
        "gerrit_sha": gerrit_sha,
        "github_sha": github_sha,
    }


def ruleset_verdict(ruleset: dict[str, Any] | None) -> dict[str, Any]:
    """Enforce the full mirror-lock contract on a GitHub ruleset object.

    ``ruleset`` is the GitHub Rulesets API object (with ``rules`` + ``bypass_actors``), or
    ``None`` when no ruleset named ``gerrit-mirror-lock-main`` exists (deleted → unprotected).
    Healthy iff the lock is active, branch-targeted at ``refs/heads/main`` only, carries all
    three rules, and its bypass is the deploy key alone.
    """
    reasons: list[str] = []
    if ruleset is None:
        reasons.append("ruleset absent — main is UNPROTECTED (deleted or never created)")
        return {
            "check": "ruleset",
            "healthy": False,
            "reason": "; ".join(reasons),
            "reasons": reasons,
        }

    if ruleset.get("enforcement") != "active":
        reasons.append("enforcement is {!r}, expected 'active'".format(ruleset.get("enforcement")))
    if ruleset.get("target") != "branch":
        reasons.append("target is {!r}, expected 'branch'".format(ruleset.get("target")))

    include = (ruleset.get("conditions") or {}).get("ref_name", {}).get("include", [])
    if include != REQUIRED_INCLUDE_REFS:
        reasons.append(f"ref_name.include is {include!r}, expected {REQUIRED_INCLUDE_REFS!r}")

    rule_types = {r.get("type") for r in (ruleset.get("rules") or [])}
    missing = REQUIRED_RULE_TYPES - rule_types
    if missing:
        reasons.append("missing rule(s): {}".format(", ".join(sorted(missing))))

    bypass = ruleset.get("bypass_actors") or []
    actor_types = {b.get("actor_type") for b in bypass}
    if actor_types != {"DeployKey"}:
        reasons.append(
            "bypass_actors must be DeployKey-only, found {}".format(sorted(actor_types) or "none")
        )

    healthy = not reasons
    return {
        "check": "ruleset",
        "healthy": healthy,
        "reason": "locked (deploy-key-only)" if healthy else "; ".join(reasons),
        "reasons": reasons,
    }


# ===========================================================================
# Thin I/O fetchers (urllib; the seams tests monkeypatch)
# ===========================================================================
def _http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted hosts)
        return resp.read()


_XSSI = ")]}'"


def _strip_xssi(body: bytes) -> str:
    """Strip Gerrit's ``)]}'`` XSSI prefix before JSON parse.

    Robust (mirrors ``review_bot.gerrit_client._strip_xssi``): lstrip, remove the exact
    4-char prefix, then strip — never assumes a fixed byte count / trailing newline.
    """
    text = body.decode("utf-8").lstrip()
    if text.startswith(_XSSI):
        text = text[len(_XSSI) :]
    return text.strip()


def fetch_gerrit_main_sha(base_url: str = GERRIT_BASE_URL) -> str:
    """Gerrit ``main`` revision via the anonymous REST API (no auth needed)."""
    body = _http_get("{}/projects/rebar/branches/main".format(base_url.rstrip("/")))
    return json.loads(_strip_xssi(body))["revision"]


def fetch_github_main_sha(repo: str = GITHUB_REPO, token: str | None = None) -> str:
    """GitHub ``main`` commit SHA (public repo — token optional, used only for rate limits)."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = _http_get(f"https://api.github.com/repos/{repo}/commits/main", headers)
    return json.loads(body)["sha"]


def fetch_github_ruleset(
    repo: str = GITHUB_REPO, name: str = LOCK_RULESET_NAME, token: str | None = None
) -> dict[str, Any] | None:
    """Fetch the named repo ruleset (with rules + bypass_actors), or ``None`` if absent.

    Requires ``administration:read`` on ``token`` — in GitHub Actions grant the job
    ``permissions: administration: read``; a fine-grained PAT is the documented fallback.
    """
    if not token:
        raise ValueError("fetch_github_ruleset requires a GitHub token with administration:read")
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
    summaries = json.loads(_http_get(f"https://api.github.com/repos/{repo}/rulesets", headers))
    match = next((r for r in summaries if r.get("name") == name), None)
    if match is None:
        return None
    # The list endpoint omits rules/bypass_actors; fetch the detail by id.
    detail = _http_get(
        "https://api.github.com/repos/{}/rulesets/{}".format(repo, match["id"]), headers
    )
    return json.loads(detail)


# ===========================================================================
# CLI
# ===========================================================================
def run(
    *,
    check_replication: bool,
    check_ruleset: bool,
    github_token: str | None,
    base_url: str = GERRIT_BASE_URL,
    repo: str = GITHUB_REPO,
) -> tuple[list[dict[str, Any]], int]:
    """Run the requested checks; return (verdicts, exit_code). Never raises for expected
    network errors — those become exit code 2 so a scheduler can distinguish drift (1) from
    a transient fetch failure (2)."""
    verdicts: list[dict[str, Any]] = []
    try:
        if check_replication:
            verdicts.append(
                replication_verdict(
                    fetch_gerrit_main_sha(base_url), fetch_github_main_sha(repo, github_token)
                )
            )
        if check_ruleset:
            verdicts.append(ruleset_verdict(fetch_github_ruleset(repo, token=github_token)))
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        verdicts.append({"check": "io", "healthy": False, "reason": f"fetch error: {exc}"})
        return verdicts, _EXIT_ERROR
    exit_code = _EXIT_HEALTHY if all(v["healthy"] for v in verdicts) else _EXIT_UNHEALTHY
    return verdicts, exit_code


def main(argv: list[str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(prog="rebar.mirror_guard", description=__doc__)
    parser.add_argument(
        "--replication", action="store_true", help="check replication (Gerrit vs GitHub main SHA)"
    )
    parser.add_argument(
        "--ruleset", action="store_true", help="check the mirror-lock ruleset for drift"
    )
    parser.add_argument(
        "--all", action="store_true", help="run all checks (default when none selected)"
    )
    parser.add_argument("--base-url", default=GERRIT_BASE_URL)
    parser.add_argument("--repo", default=GITHUB_REPO)
    args = parser.parse_args(argv)

    do_all = args.all or not (args.replication or args.ruleset)
    verdicts, code = run(
        check_replication=do_all or args.replication,
        check_ruleset=do_all or args.ruleset,
        github_token=os.environ.get("GITHUB_TOKEN"),
        base_url=args.base_url,
        repo=args.repo,
    )
    json.dump({"exit_code": code, "verdicts": verdicts}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
