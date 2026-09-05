#!/usr/bin/env python3
# mechanism-ok: ci_gate scripts/check_user_data_size.py — bug a68c-9633-248c-4b06: the EC2
# 16 KiB UserData cap is enforced by AWS at PLAN time, so exceeding it makes terraform unable
# to generate a plan for the WHOLE configuration and blocks every apply. Nothing in the repo
# measured that payload, so the only feedback was a broken plan a day later, read as "drift".
"""Size gate for EC2 ``user_data`` payloads [rebar:a68c-9633-248c-4b06].

EC2 caps ``UserData`` at **16,384 bytes**, and the AWS provider validates it. When
``infra/terraform/user_data.sh`` grew past that cap, ``terraform plan`` stopped being able to
GENERATE -- for the whole configuration, not just the instance -- because terraform evaluates
every resource before it reports anything. Every apply was blocked for a day, and the daily
drift sweep's red run read as "there is drift" rather than "there is no plan".

This gate moves that discovery to commit time. It is deliberately NOT a line-count or
file-size lint:

**It measures the payload AWS actually receives, not the file on disk.** That distinction is
load-bearing in BOTH directions, and each direction was measured on the change that motivated
this gate:

* ``templatefile()`` interpolation makes the rendered script a DIFFERENT size from the raw
  file -- 16,666 bytes raw against 16,668 rendered. A raw-file guard therefore UNDER-reports
  and can pass a configuration terraform will reject. The gap is two bytes here only because
  the substituted values happen to be near the length of the ``${...}`` references they
  replace; it grows with every interpolation added.
* When the HCL wraps the render in ``base64gzip()`` -- which is how this repo now stays under
  the cap without deleting the script's load-bearing documentation -- the transported payload
  is 6,941 bytes for the same 16,668-byte render. A raw-file guard would OVER-report by a
  factor of 2.4 and fail every build forever.

So neither the raw file nor the rendered script is the right quantity. The right quantity is
the bytes handed to the EC2 API: the render, then whatever encoding the HCL declares.

**Conservative by construction.** The real values of ``${...}`` variables are only known to
terraform (they are resource attributes such as ``aws_ebs_volume.data.id``). Each is therefore
substituted with an incompressible placeholder of ``PLACEHOLDER_LEN`` bytes -- longer than any
value this repo passes (EBS volume ids are 21 characters, the scratch mount path 27) -- so the
gate's answer is an UPPER BOUND on the true payload. Incompressible rather than repetitive on
purpose: a run of identical padding bytes would gzip away to nothing and quietly turn the
bound back into an under-estimate.

The cost of that bound is stated plainly: the gate can fail slightly BEFORE terraform does.
On the change that motivated it, terraform reported 284 bytes over and this gate reported 536,
because three interpolations were padded to 64 bytes each. Erring that way is the correct side
-- a gate that trails the real limit passes a configuration that cannot be planned, which is
precisely the failure being prevented.

**A call site it cannot check must not read as a clean one.** An unresolvable template path is
reported and fails the gate, matching ``check_templatefile_escapes.py``'s stance -- a silent
skip is how a guard becomes vacuous.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# `scripts/` is not a package, so this sibling import resolves only while that directory is on
# `sys.path`. It is under `python scripts/<x>.py`, and it is during a FULL test session because
# tests/scripts/conftest.py inserts it process-wide -- but NOT under a subset run or an
# importlib load, which is how a module can pass CI and fail when exercised directly
# (bug 291e-7b48-3f24-41c6). Derived from __file__ so it holds under every invocation style.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_templatefile_escapes import (  # noqa: E402
    EXCLUDED_DIRS,
    declared_vars,
    resolve_template_path,
)

USER_DATA_LIMIT_BYTES = 16384
"""EC2's hard cap on ``UserData``, applied to the DECODED bytes (AWS EC2 API reference)."""

PLACEHOLDER_LEN = 64
"""Substituted length for an unknown interpolation value. See the module docstring."""

#: `user_data = ...` / `user_data_base64 = ...`, capturing the start of the value expression.
_USER_DATA_ATTR = re.compile(r"^\s*(user_data|user_data_base64)\s*=\s*(.+)$", re.MULTILINE)


def placeholder(name: str) -> str:
    """A high-entropy ``PLACEHOLDER_LEN``-byte stand-in, stable per variable name.

    Built by CHAINING digests rather than repeating one: a repeated digest is a periodic
    string that DEFLATE collapses to a back-reference, which would make the padding cost
    nothing and turn the intended upper bound into an under-estimate. The bound is verified
    end-to-end against the real interpolated values by
    ``test_the_estimate_is_at_least_the_payload_built_from_the_real_values``.
    """
    chunks: list[str] = []
    seed = name.encode()
    while sum(len(c) for c in chunks) < PLACEHOLDER_LEN:
        seed = hashlib.sha256(seed).digest()
        chunks.append(base64.b64encode(seed).decode())
    return "".join(chunks)[:PLACEHOLDER_LEN]


def render(text: str, values: dict[str, str]) -> str:
    """Apply Terraform ``templatefile()`` semantics: ``$${`` is literal, ``${name}`` expands."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("$${", i) or text.startswith("%%{", i):
            out.append(text[i + 1 : i + 3])
            i += 3
        elif text.startswith("${", i):
            end = text.find("}", i)
            if end < 0:
                out.append(text[i:])
                break
            out.append(values.get(text[i + 2 : end].strip(), ""))
            i = end + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


@dataclass(frozen=True)
class Payload:
    """One ``user_data`` call site, measured as AWS will receive it."""

    tf_file: str
    attribute: str
    template: str
    rendered_bytes: int
    transport_bytes: int
    encoding: str

    @property
    def over(self) -> bool:
        return self.transport_bytes > USER_DATA_LIMIT_BYTES

    def render_report(self) -> str:
        headroom = USER_DATA_LIMIT_BYTES - self.transport_bytes
        used = 100 * self.transport_bytes // USER_DATA_LIMIT_BYTES
        verdict = (
            f"OVER by {-headroom} bytes"
            if self.over
            else f"{headroom} bytes of headroom ({used}% used)"
        )
        return (
            f"{self.tf_file}: {self.attribute} <- {self.template}\n"
            f"    rendered {self.rendered_bytes} B, transported {self.transport_bytes} B "
            f"({self.encoding}), limit {USER_DATA_LIMIT_BYTES} B -- {verdict}"
        )


@dataclass(frozen=True)
class Unresolved:
    """A ``user_data`` call site the gate could not measure. Fails, never skipped."""

    tf_file: str
    attribute: str
    detail: str

    def render_report(self) -> str:
        return (
            f"{self.tf_file}: {self.attribute} was NOT measured ({self.detail}). An unchecked "
            "call site must not pass silently -- make the template path resolvable."
        )


def measure(tf_file: Path, root: Path) -> list[Payload | Unresolved]:
    """Measure every ``user_data``/``user_data_base64`` attribute in one ``.tf`` file."""
    text = tf_file.read_text(encoding="utf-8")
    rel = str(tf_file.relative_to(root))
    results: list[Payload | Unresolved] = []
    for attr in _USER_DATA_ATTR.finditer(text):
        name, expression = attr.group(1), attr.group(2)
        call = re.search(r'templatefile\(\s*"([^"]*)"\s*,', expression)
        if call is None:
            # A literal or a non-templatefile expression: nothing this gate can render.
            continue
        template = resolve_template_path(call.group(1), tf_file, root)
        if template is None:
            results.append(Unresolved(rel, name, f"cannot resolve {call.group(1)!r}"))
            continue
        variables = declared_vars(text, attr.start(2) + call.end())
        body = render(
            template.read_text(encoding="utf-8"), {v: placeholder(v) for v in variables}
        ).encode("utf-8")
        gzipped = expression.lstrip().startswith("base64gzip(")
        transport = gzip.compress(body, mtime=0) if gzipped else body
        results.append(
            Payload(
                rel,
                name,
                str(template.relative_to(root)),
                len(body),
                len(transport),
                "gzipped" if gzipped else "plain",
            )
        )
    return results


def check_repo(root: Path) -> list[Payload | Unresolved]:
    """Every ``user_data`` call site under ``root``."""
    found: list[Payload | Unresolved] = []
    for tf_file in sorted(root.rglob("*.tf")):
        if EXCLUDED_DIRS.intersection(tf_file.relative_to(root).parts):
            continue
        found.extend(measure(tf_file, root))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    results = check_repo(args.root)
    for result in results:
        print(result.render_report())
    bad = [r for r in results if isinstance(r, Unresolved) or r.over]
    if bad:
        print(
            f"\nFAIL: {len(bad)} user_data payload(s) exceed the {USER_DATA_LIMIT_BYTES}-byte "
            "EC2 limit or could not be measured. terraform plan CANNOT GENERATE while this "
            "holds -- for the whole configuration, not just the instance. Wrap the render in "
            "base64gzip() (cloud-init decompresses it), or move the script to S3. Do NOT "
            "delete the script's comments: templatefile() interpolates them, so they are the "
            "context that stops the next editor re-breaking it.",
            file=sys.stderr,
        )
        return 1
    if not results:
        print("no user_data templatefile() call sites found", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
