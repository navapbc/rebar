#!/usr/bin/env python3
"""CI drift-guard: keep server.json's advertised env-var contract in sync with code.

WHY THIS EXISTS
---------------
``server.json`` is the MCP front-door manifest (what an MCP client shows a user
before install). It once advertised only a stale subset of the real environment
gates — it listed the *deprecated* ``REBAR_MCP_ALLOW_RECONCILE_LIVE`` while
omitting ``REBAR_MCP_ALLOW_LLM`` and ``REBAR_MCP_ALLOW_JIRA_SYNC`` entirely, so
the LLM / Jira-sync tools appeared but failed at call time with no manifest hint.

``rebar.mcp_server.MCP_ENV_VARS`` is the SINGLE SOURCE OF TRUTH for the env vars
the server honors. This guard diffs ``server.json``'s advertised
``environmentVariables`` against that canonical list and fails the build on any
divergence (missing, extra, or renamed var), so the manifest can never silently
drift again. Style mirrors the prompt-index / criteria-routing drift gates in
``.github/workflows/test.yml``.

To fix a failure: regenerate the env block from the canonical list, e.g.

    python - <<'PY'
    import json, rebar.mcp_server as m
    d = json.load(open("server.json"))
    d["packages"][0]["environmentVariables"] = [
        {"name": v["name"], "description": v["description"], "isRequired": False}
        for v in m.MCP_ENV_VARS
    ]
    json.dump(d, open("server.json", "w"), indent=2, ensure_ascii=False)
    open("server.json", "a").write("\n")
    PY

and commit the result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rebar.mcp_server import MCP_ENV_VARS

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_JSON = REPO_ROOT / "server.json"


def manifest_env_names() -> list[str]:
    """The env-var names advertised by server.json's first package."""
    data = json.loads(SERVER_JSON.read_text())
    packages = data.get("packages") or []
    if not packages:
        raise SystemExit("server.json: no 'packages' entry to read environmentVariables from")
    return [e["name"] for e in packages[0].get("environmentVariables", [])]


def main() -> int:
    canonical = [v["name"] for v in MCP_ENV_VARS]
    advertised = manifest_env_names()

    canon_set, adv_set = set(canonical), set(advertised)
    missing = canon_set - adv_set  # honored in code but not advertised
    extra = adv_set - canon_set  # advertised but not a real gate

    if missing or extra:
        print(
            "::error::server.json env-var contract has drifted from rebar.mcp_server.MCP_ENV_VARS"
        )
        if missing:
            print(f"  MISSING from server.json (real gates not advertised): {sorted(missing)}")
        if extra:
            print(f"  EXTRA in server.json (advertised but not a real gate): {sorted(extra)}")
        print("  Regenerate server.json's environmentVariables from MCP_ENV_VARS")
        print("  (see this script's docstring for the one-liner).")
        return 1

    print(f"server.json env contract: OK ({len(canonical)} vars in sync with MCP_ENV_VARS).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
