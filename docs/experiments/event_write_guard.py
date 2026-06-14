#!/usr/bin/env python3
"""Structural guard for P1.0: fail CI if any event-file write bypasses the canonical
serializer. Validated by EXP-R9/R9b — catches all live+dead event writers with ZERO
false positives across 73 read/output json.dumps sites, using only the stdlib (the
repo's CI has no semgrep/ast-grep/pre-commit — verified absent).

Two prongs, because no linter parses Python embedded in bash heredocs:
  * Python (AST): flag json.dump(s) whose first arg is a dict literal carrying an
    "event_type" key, or a variable assigned such a dict. Skips read/output dumps.
  * Bash (regex): flag .sh files that contain BOTH json.dump AND an 'event_type'
    dict — the read/output .sh (issue-summary, clarity, scratch, fsck, …) have
    json.dump but no event_type, so they're correctly ignored.

Allowlist the one canonical helper and the dead/migration scripts (which P1.0
deletes or leaves frozen). Run:  python3 docs/experiments/event_write_guard.py src/rebar
Exit 1 if any non-allowlisted raw event serializer is found.
"""
import ast, os, re, sys

PY_ALLOW = {"_store/event_append.py"}            # the canonical helper itself
SH_ALLOW_SUBSTR = ("ticket-create.sh", "ticket-edit.sh", "ticket-migrate")  # dead/one-shot
SH_EVENT = re.compile(r"""['"]event_type['"]""")
SH_DUMP = re.compile(r"json\.dumps?\s*\(")


def _has_event_type(node):
    return isinstance(node, ast.Dict) and any(
        isinstance(k, ast.Constant) and k.value == "event_type" for k in node.keys
    )


def scan_python(root):
    hits = []
    for dp, _, fs in os.walk(root):
        for fn in fs:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            if any(p.endswith(a) for a in PY_ALLOW):
                continue
            try:
                tree = ast.parse(open(p, encoding="utf-8").read())
            except SyntaxError:
                continue
            evvars = {
                t.id
                for n in ast.walk(tree)
                if isinstance(n, ast.Assign) and _has_event_type(n.value)
                for t in n.targets
                if isinstance(t, ast.Name)
            }
            for n in ast.walk(tree):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("dump", "dumps")
                ):
                    a0 = n.args[0] if n.args else None
                    if _has_event_type(a0) or (isinstance(a0, ast.Name) and a0.id in evvars):
                        hits.append(f"{p}:{n.lineno}")
    return hits


def scan_bash(root):
    hits = []
    for dp, _, fs in os.walk(root):
        for fn in fs:
            if not fn.endswith(".sh") or any(s in fn for s in SH_ALLOW_SUBSTR):
                continue
            p = os.path.join(dp, fn)
            body = open(p, encoding="utf-8", errors="replace").read()
            if SH_DUMP.search(body) and SH_EVENT.search(body):
                hits.append(p)
    return hits


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "src/rebar"
    py, sh = scan_python(root), scan_bash(root)
    for h in sorted(py):
        print(f"PY  raw event serializer (must use canonical_bytes): {h}")
    for h in sorted(sh):
        print(f"SH  event-write heredoc (must call canonical helper): {h}")
    # During migration these are the KNOWN writers P1.0 conforms; once conformed,
    # the guard's expected set should be EMPTY (all route through the helper).
    print(f"\nfound {len(py)} python + {len(sh)} bash event-write sites")
    sys.exit(1 if (py or sh) else 0)
