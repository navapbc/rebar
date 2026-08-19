"""Behavioral conformance-oracle PROTOTYPE for rebar write-op parity.

One transport-agnostic contract (a table of cases) run through the lib and MCP
adapters against a temp store. Classifies each surface's response as:
  ACCEPTED         — core performed the op
  REJECTED         — core refused by a runtime rule (RebarError/Concurrency)
  PARAM_NOT_EXPOSED — the surface cannot even express the input (TypeError)
Divergence between the two surfaces on the SAME case == a parity gap.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile

# ---- temp store bootstrap (mirrors tests/interfaces/conftest.py) --------------
_d = tempfile.mkdtemp()
repo = os.path.join(_d, "repo")
os.makedirs(repo)
subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
os.environ["REBAR_ROOT"] = repo
os.environ["XDG_CONFIG_HOME"] = os.path.join(_d, "xdg")
for v in ("REBAR_TRACKER_DIR", "REBAR_TRACKER_BRANCH", "REBAR_CONFIG"):
    os.environ.pop(v, None)
os.chdir(repo)

import rebar
from rebar import _mcp_writes, config as _cfg

_cfg.reset_config_cache()
rebar.init_repo(repo_root=repo)
subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "init"], cwd=repo, check=True)

# ---- capture MCP write-tool callables (mirrors tests/unit/test_mcp_writes.py) -
_tools: dict = {}


class _FakeMCP:
    def tool(self, *_a, **_k):
        def _dec(fn):
            _tools[fn.__name__] = fn
            return fn

        return _dec


class _FakeCtx:
    logger = logging.getLogger("proto")

    @staticmethod
    def readonly() -> bool:
        return False

    @staticmethod
    def dump(o):
        return o

    @staticmethod
    def allow_llm() -> bool:
        return False


_mcp_writes.register_write_tools(_FakeMCP(), ctx=_FakeCtx())
mcp_transition = _tools["transition_ticket"]


def _classify(fn, **kw):
    try:
        fn(**kw)
        return "ACCEPTED", ""
    except TypeError as e:
        return "PARAM_NOT_EXPOSED", str(e).split("\n")[0]
    except Exception as e:  # RebarError / ConcurrencyError etc.
        return "REJECTED", f"{type(e).__name__}: {str(e).splitlines()[0][:80]}"


def _fresh_bug_in_progress(title: str) -> str:
    tid = rebar.create_ticket("bug", title)
    rebar.transition(tid, "open", "in_progress")
    return tid


def lib_close(tid, **extra):
    return rebar.transition(tid, "in_progress", "closed", **extra)


def mcp_close(tid, **extra):
    return mcp_transition(
        ticket_id=tid, current_status="in_progress", target_status="closed", **extra
    )


print("=" * 78)
print("CASE A — bug close MISSING close_class (runtime rule: required iff bug-close)")
print("=" * 78)
a1 = _fresh_bug_in_progress("caseA-lib")
a2 = _fresh_bug_in_progress("caseA-mcp")
print("  lib:", _classify(lib_close, tid=a1))
print("  mcp:", _classify(mcp_close, tid=a2))

print("=" * 78)
print("CASE B — bug close close_class=not_a_bug WITHOUT reason (reason-required class)")
print("=" * 78)
b1 = _fresh_bug_in_progress("caseB-lib")
b2 = _fresh_bug_in_progress("caseB-mcp")
print("  lib:", _classify(lib_close, tid=b1, close_class="not_a_bug"))
print("  mcp:", _classify(mcp_close, tid=b2, close_class="not_a_bug"))

print("=" * 78)
print("CASE C — bug close close_class=not_a_bug WITH reason (the COMPLIANT close)")
print("=" * 78)
c1 = _fresh_bug_in_progress("caseC-lib")
c2 = _fresh_bug_in_progress("caseC-mcp")
print("  lib:", _classify(lib_close, tid=c1, close_class="not_a_bug", reason="operator: intended"))
print("  mcp:", _classify(mcp_close, tid=c2, close_class="not_a_bug", reason="operator: intended"))

print("=" * 78)
print("CASE D — caused_by / ref params on a bug close")
print("=" * 78)
d1 = _fresh_bug_in_progress("caseD-lib")
print("  lib caused_by:", _classify(lib_close, tid=d1, close_class="regression", caused_by="deadbeef"))
d2 = _fresh_bug_in_progress("caseD-mcp")
print("  mcp caused_by:", _classify(mcp_close, tid=d2, close_class="regression", caused_by="deadbeef"))

print("=" * 78)
print("CASE E — force on start-work transition (open -> in_progress)")
print("=" * 78)
e1 = rebar.create_ticket("task", "caseE-lib")
print("  lib force:", _classify(lambda **k: rebar.transition(**k), ticket_id=e1,
      current_status="open", target_status="in_progress", force=True, reason="ops call"))
e2 = rebar.create_ticket("task", "caseE-mcp")
print("  mcp force:", _classify(mcp_transition, ticket_id=e2,
      current_status="open", target_status="in_progress", force=True))
