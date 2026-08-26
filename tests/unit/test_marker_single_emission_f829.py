"""One real error -> exactly ONE probe-countable marker line (bug f829-152a-b415-44a4).

The host observability probe counts markers with an anchored grep over the container's
journald stream (``infra/scripts/observability.sh``: ``grep -cE '^<TOKEN> \\{'``), and
journald ingests BOTH the container's stdout and stderr as line-start messages. Each
marker emitter used to write the token-prefixed line on TWO paths — a ``logger`` call
(-> the ``configure_logging()`` stdout handler) AND a ``print`` to stderr (the
misconfigured-logging fail-safe) — so with logging configured every real error was
counted twice. The contract: the unconditional stderr ``print`` is the ONLY line-start
marker emission; the logger copy logs the structured record body WITHOUT the token
prefix, so it can never match the anchor, while the fail-safe still yields exactly one
line when logging is unconfigured.

The test drives each emitter in a real subprocess and counts anchor matches across
stdout+stderr combined — the journald view — in both logging modes.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from _tree_scan import parsed_python_files

REPO_SRC = Path(__file__).resolve().parents[2] / "src"

# One driver snippet per marker emitter. Each stubs ONLY the irreducible AWS boundary
# (the best-effort CloudWatch publish helper) and invokes the real emitter.
_EMITTERS: dict[str, tuple[str, str]] = {
    "VOTER_ERROR": (
        "import rebar.review_bot.voter as m\n"
        "m._publish_voter_error_metric = lambda: None\n"
        "m._voter_error(change_id='Itest', revision_id='r1', vote_value=-1,"
        " http_status=500, error='boom')\n"
    ),
    "LLM_TOKEN_USAGE": (
        "import rebar.review_bot.voter as m\n"
        "m._publish_token_usage_metrics = lambda metrics: None\n"
        "m._emit_token_usage('Itest', 'r1', {'input_tokens': 1})\n"
    ),
    "MERGE_CHANGE_ERROR": (
        "import rebar.review_bot.voter_merge as m\n"
        "m.publish_merge_change_error_metric = lambda reason: None\n"
        "m.merge_change_error('merge_files_error', 'files', change_id='Itest')\n"
    ),
    "RECONCILE_DEGRADED": (
        "import rebar.review_bot.reconcile as m\nm._degraded('holdback_expired', detail='test')\n"
    ),
    "ARTIFACT_EMIT_ERROR": (
        "import rebar.review_bot.artifact_emit as m\n"
        "m._publish_artifact_emit_error_metric = lambda: None\n"
        "m._artifact_emit_error(change_id='Itest', error='boom')\n"
    ),
}

_CONFIGURE = "from rebar.review_bot.config import configure_logging\nconfigure_logging()\n"


def _run_emitter(token: str, *, configured: bool) -> tuple[int, int]:
    """Run one emitter in a fresh interpreter; return anchored-line counts on
    (stdout, stderr) — journald counts the union of both."""
    driver = (_CONFIGURE if configured else "") + _EMITTERS[token]
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    anchor = re.compile(rf"^{token} \{{", re.MULTILINE)
    return len(anchor.findall(proc.stdout)), len(anchor.findall(proc.stderr))


@pytest.mark.parametrize("token", sorted(_EMITTERS))
def test_configured_logging_emits_exactly_one_countable_marker_line(token: str) -> None:
    """With the production logging config wired (as ``rebar.review_bot.app`` does at
    import), one real error must produce exactly ONE line matching the observability
    probe's anchor across the process's stdout+stderr."""
    n_out, n_err = _run_emitter(token, configured=True)
    assert n_out + n_err == 1, (
        f"{token}: probe would count {n_out + n_err} lines for ONE real error "
        f"(stdout={n_out}, stderr={n_err}); the alarm metric doubles (bug f829-152a)"
    )


@pytest.mark.parametrize("token", sorted(_EMITTERS))
def test_misconfigured_logging_fallback_still_emits_one_marker_line(token: str) -> None:
    """The fail-safe: with NO logging configured (uvicorn default — no ``rebar``
    handler), the stderr print must still land exactly one countable marker line."""
    n_out, n_err = _run_emitter(token, configured=False)
    assert (n_out, n_err) == (0, 1), (
        f"{token}: misconfigured-logging fallback emitted stdout={n_out}, "
        f"stderr={n_err}; expected exactly one stderr marker line"
    )


def test_no_token_prefixed_marker_string_reaches_a_logger_call() -> None:
    """Structural class guard: no ``src/rebar`` module may pass a line-start
    token-prefixed marker string (``"<TOKEN> " + json.dumps(...)``) to a ``logger``
    call — that is the exact dual-emission construct that doubled the voter_errors
    metric. The stderr ``print`` is the single sanctioned line-start emission."""
    assignment = re.compile(r"^\s*(\w+)\s*=\s*[\"']([A-Z][A-Z0-9_]{2,}) [\"']\s*\+")
    violations: list[str] = []
    for module in parsed_python_files(REPO_SRC):
        lines = module.source.splitlines()
        for i, line in enumerate(lines):
            m = assignment.match(line)
            if not m:
                continue
            var, token = m.group(1), m.group(2)
            logger_call = re.compile(rf"logger\.\w+\(\s*{re.escape(var)}\s*[),]")
            for j in range(i + 1, min(i + 12, len(lines))):
                if logger_call.search(lines[j]):
                    violations.append(f"{module.path.relative_to(REPO_SRC)}:{j + 1} ({token})")
                    break
    assert not violations, (
        "token-prefixed marker line passed to a logger call (dual line-start emission; "
        "the observability anchor counts it twice — bug f829-152a): " + ", ".join(violations)
    )
