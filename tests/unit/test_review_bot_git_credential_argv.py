"""No credential reaches a git or curl command line (bug antiviral-agrarian-amurminnow).

A process's argv is readable by anyone who can list processes on the host or in the
container (``ps``, ``/proc/<pid>/cmdline``, SSM command output, monitoring agents). The
review-bot used to put ``bot_user:token@`` in its ``git fetch`` URL, and a ``ps`` sample on
the production host showed it. The bot now hands git its Basic header through git's own
``GIT_CONFIG_*`` environment, scoped to the Gerrit URL. The first group of tests drives
``clone_change_ref`` through a recording ``subprocess.run``; the last scans ``src/rebar``
for the old URL-userinfo shape and for an auth header passed as a ``-c`` argument.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.parse
from pathlib import Path
from unittest import mock

import pytest

from rebar.review_bot import gerrit_client as gc_mod
from rebar.review_bot.config import ReceiverConfig

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_USER = "rebar-review-bot"
_TOKEN = "s3cr3t/bot+token=="
_BASE = "https://gerrit.example.test"


def _client(tmp_path: Path) -> gc_mod.GerritClient:
    return gc_mod.GerritClient(
        ReceiverConfig(
            llm_review_max_value=1,
            llm_review_block_value=-1,
            dedup_db_path=str(tmp_path / "voted.db"),
            gerrit_base_url=_BASE,
            bot_user=_USER,
            gerrit_bot_token=_TOKEN,
            webhook_token="tok",
            project="rebar",
        )
    )


def _secret_forms() -> list[str]:
    basic = base64.b64encode(f"{_USER}:{_TOKEN}".encode()).decode("ascii")
    return [_TOKEN, urllib.parse.quote(_TOKEN, safe=""), basic]


def _record_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]):
    """Run ``clone_change_ref`` against a recording ``subprocess.run``; return its calls."""
    for name in [n for n in list(gc_mod.os.environ) if n.startswith("GIT_CONFIG_")]:
        monkeypatch.delenv(name)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    calls: list[dict] = []

    def _fake_run(cmd, **kwargs):
        calls.append({"cmd": [str(part) for part in cmd], "env": kwargs.get("env")})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    # gerrit_client does a plain ``import subprocess``: scope the patch with ``with`` so
    # the stdlib attribute is restored before any fixture teardown shells out.
    with mock.patch.object(gc_mod.subprocess, "run", _fake_run):
        _client(tmp_path).clone_change_ref(42, "refs/changes/42/42/1", str(tmp_path / "d"))
    assert calls, "clone_change_ref made no subprocess calls"
    return calls


def _config_entries(env: dict[str, str]) -> list[tuple[str, str]]:
    count = int(env["GIT_CONFIG_COUNT"])
    return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)]


def _gerrit_fetch(calls: list[dict]) -> dict:
    return next(c for c in calls if "fetch" in c["cmd"] and "origin" not in c["cmd"])


def test_no_git_argv_carries_the_bot_credential(tmp_path, monkeypatch):
    calls = _record_clone(tmp_path, monkeypatch, {})
    for call in calls:
        argv = " ".join(call["cmd"])
        for secret in _secret_forms():
            assert secret not in argv, f"credential form in git argv: {call['cmd'][:4]}..."
    fetch = _gerrit_fetch(calls)
    assert f"{_BASE}/a/rebar" in fetch["cmd"], "fetch must target the userinfo-free URL"


def test_the_gerrit_fetch_authenticates_through_url_scoped_env_config(tmp_path, monkeypatch):
    fetch = _gerrit_fetch(_record_clone(tmp_path, monkeypatch, {}))
    env = fetch["env"]
    assert env is not None, "the Gerrit fetch must get its auth through the environment"
    basic = base64.b64encode(f"{_USER}:{_TOKEN}".encode()).decode("ascii")
    assert _config_entries(env) == [(f"http.{_BASE}/.extraHeader", f"Authorization: Basic {basic}")]
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_inherited_git_config_entries_are_kept_ahead_of_the_header(tmp_path, monkeypatch):
    # The review-bot container injects safe.directory this way (docker-compose.yml).
    inherited = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "/var/gerrit/site/reviewbot-tickets",
    }
    env = _gerrit_fetch(_record_clone(tmp_path, monkeypatch, inherited))["env"]
    entries = _config_entries(env)
    assert entries[0] == ("safe.directory", "/var/gerrit/site/reviewbot-tickets")
    assert entries[1][0] == f"http.{_BASE}/.extraHeader"
    assert len(entries) == 2


def test_a_malformed_inherited_count_does_not_crash_the_fetch(tmp_path, monkeypatch):
    env = _gerrit_fetch(_record_clone(tmp_path, monkeypatch, {"GIT_CONFIG_COUNT": "x"}))["env"]
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert _config_entries(env)[0][0] == f"http.{_BASE}/.extraHeader"


def test_no_other_git_call_receives_the_gerrit_header(tmp_path, monkeypatch):
    calls = _record_clone(tmp_path, monkeypatch, {})
    fetch = _gerrit_fetch(calls)
    for call in calls:
        if call is fetch:
            continue
        env = call["env"] or {}
        assert not any("Authorization" in str(v) for v in env.values()), call["cmd"][:4]


def test_the_header_is_scoped_to_gerrit_by_git_itself(tmp_path, monkeypatch):
    """Real git resolves the URL-scoped entry for Gerrit and for no other host.

    The lookup runs outside any repository with global and system config off: a CI
    checkout persists its own ``http.https://github.com/.extraheader``, which this test
    must neither read nor print."""
    env = dict(_gerrit_fetch(_record_clone(tmp_path, monkeypatch, {}))["env"])
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES=str(tmp_path)
    )
    outside = tmp_path / "outside"
    outside.mkdir()

    def lookup(url: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "config", "--get-urlmatch", "http.extraheader", url],
            cwd=outside,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    assert lookup(f"{_BASE}/a/rebar").stdout.startswith("Authorization: Basic ")
    assert lookup("https://github.com/navapbc/rebar").returncode == 1


# ── static guard over src/rebar ─────────────────────────────────────────────────────

# A credential-named value interpolated just before ``@``: URL userinfo, the old shape.
_USERINFO_SECRET = re.compile(r"\{[^{}]*(?:token|password|passwd|secret|_pat)[^{}]*\}@", re.I)
# A git auth header assembled as a command-line ``-c`` value instead of in the environment.
_ARGV_HEADER = re.compile(r"""["']-c["']\s*,\s*f?["'][^"']*(?:extraheader|authorization)""", re.I)


def test_the_patterns_flag_the_shapes_they_exist_to_catch():
    assert _USERINFO_SECRET.search("f\"{quote(self._cfg.gerrit_bot_token, safe='')}@\"")
    assert _USERINFO_SECRET.search('f"https://{user}:{password}@{host}"')
    assert not _USERINFO_SECRET.search('f"{self._base}/a/{project}"')
    assert _ARGV_HEADER.search('["git", "-c", f"http.extraHeader=Authorization: {x}", "fetch"]')
    assert not _ARGV_HEADER.search('["git", "-c", "http.lowSpeedLimit=1000", "fetch"]')


def test_no_source_file_puts_a_credential_in_a_url_or_argv():
    hits = []
    for path in sorted((_REPO_ROOT / "src" / "rebar").rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _USERINFO_SECRET.search(line) or _ARGV_HEADER.search(line):
                hits.append(f"{path.relative_to(_REPO_ROOT)}:{n}")
    assert not hits, f"credential in a URL or command line (pass it via GIT_CONFIG_* env): {hits}"


# ── static guard over operator scripts, workflows and docs ──────────────────────────
# Bug immoral-heated-doctorfish: the same class outside src/rebar. Operator tooling and
# the guidance people copy from put Gerrit and Jira credentials on curl, git, ssh, aws and
# awk command lines. A credential is a ``$``-expanded value or a ``<pw>``-style placeholder.

_OPERATOR_GLOBS = (
    "infra/**/*.sh",
    "infra/**/*.md",
    "scripts/**/*.sh",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    "docs/**/*.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
)
_SECRET_NAME = r"\w*(?:token|pass|secret|pw|pat|key)\w*"
_OPERATOR_PATTERNS = {
    # curl -u / --user with a credential after the colon.
    "curl-user": re.compile(r"""(?<![\w-])(?:-u|--user)(?:\s+|=)["']?[^\s"']*:(?:\$|<)""", re.I),
    # https://user:$PW@host or https://user:<pw>@host.
    "url-userinfo": re.compile(r"""://[^/\s:@"'<>]+:(?:\$|<)[^@\s"']*@"""),
    # Gerrit SSH set/create-account with the password as an argument.
    "http-password-arg": re.compile(r"--http-password[\s=]"),
    # aws ... --value "$TOKEN" / "$(cat key)" / "$(openssl rand ...)": use file://.
    "cli-value": re.compile(
        rf"""--value(?:\s+|=)["']?\$(?:\((?:cat|openssl)\b|\{{?{_SECRET_NAME}\b)""", re.I
    ),
    # awk -v tok="$TOKEN".
    "awk-var": re.compile(rf"""-v\s+\w+=["']?\$\{{?{_SECRET_NAME}""", re.I),
    # An auth or token header built from a variable on the command line. The IMDSv2
    # session header is excluded: a short-lived, instance-local token, not a credential.
    "header-arg": re.compile(
        r"""(?:-H|--header)(?:\s+|=)["'](?!X-aws-ec2-metadata-token)[^"']*"""
        r"""(?:authorization|token|bearer)[^"']*\$""",
        re.I,
    ),
}


def _operator_files() -> list[Path]:
    seen: dict[str, Path] = {}
    for pattern in _OPERATOR_GLOBS:
        for path in _REPO_ROOT.glob(pattern):
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if path.is_file():
                seen[rel] = path
    return [seen[rel] for rel in sorted(seen)]


def _operator_hits(text: str) -> list[str]:
    return [name for name, pat in _OPERATOR_PATTERNS.items() if pat.search(text)]


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ('curl -sS -u "${BOT_USER}:${TOKEN}" "https://h/a/x"', "curl-user"),
        ('curl --user "$gerrit_user:$gerrit_password" https://h/a/', "curl-user"),
        ("`curl -u rebar-review-bot:<pw> https://h/a/accounts/self`", "curl-user"),
        ('git clone -q "https://${BOT_USER}:${TOKEN}@${GERRIT_HOST}/a/p" d', "url-userinfo"),
        ('gerrit set-account "$BOT_USER" --http-password "$TOKEN"', "http-password-arg"),
        ('--value "$TOKEN" >/dev/null', "cli-value"),
        ('--value "$(cat ~/.ssh/rebar_ci_gerrit)"', "cli-value"),
        ('--value "$(openssl rand -base64 36 | head -c 48)"', "cli-value"),
        ("awk -v tok=\"$TOKEN\" '{ gsub(/x/, tok); print }'", "awk-var"),
        ('-H "X-Rebar-Token: $TOKEN" \\', "header-arg"),
        ('--header "Authorization: Bearer ${PAT}"', "header-arg"),
    ],
)
def test_operator_patterns_flag_the_shapes_they_exist_to_catch(line, name):
    assert name in _operator_hits(line)


@pytest.mark.parametrize(
    "line",
    [
        'printf \'user = "%s:%s"\\n\' "$U" "$P" | curl -sS -K - https://h/a/x',
        'curl_auth "$BOT_USER" "$TOKEN" -sS "https://${GERRIT_HOST}/a$1"',
        'git_auth "https://${GERRIT_HOST}" "$U" "$P" clone -q "https://${GERRIT_HOST}/a/p" d',
        'GIT_HTTPS_URL="https://${GERRIT_USER}@${GERRIT_HOST}/a/${PROJECT}"',
        'gerrit set-account "$BOT_USER" --generate-http-password',
        "--value file:///dev/stdin >/dev/null",
        '--metric-name gerrit_healthy --unit Count --value "$gerrit_ok"',
        '-H "X-aws-ec2-metadata-token: $TOKEN"',
        "-H 'Content-Type: application/json' -d \"${2:-{}}\"",
        'diff -u "$before" "$after" >&2 || true',
    ],
)
def test_operator_patterns_pass_the_fixed_and_unrelated_shapes(line):
    assert _operator_hits(line) == []


def test_the_operator_scan_covers_the_files_named_by_the_bug():
    scanned = {p.relative_to(_REPO_ROOT).as_posix() for p in _operator_files()}
    for rel in (
        "infra/gerrit/service-user.sh",
        "infra/gerrit/smoke-check.sh",
        "infra/gerrit/reviewbot-e2e.sh",
        "infra/gerrit/feature-branch-e2e.sh",
        "infra/gerrit/feature-branch-e2e-scenarios.sh",
        "infra/gerrit/feature-branch-inventory.sh",
        ".github/workflows/external-integration.yml",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "infra/runbooks/gerrit-auth-hardening.md",
        # The MCP client PAT setup guide (bug limitless-defaceable-beaver).
        "docs/mcp-client-setup.md",
    ):
        assert rel in scanned, rel


def test_no_operator_script_or_doc_puts_a_credential_in_argv():
    hits = []
    for path in _operator_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            hits.extend(f"{rel}:{n} [{name}]" for name in _operator_hits(line))
    assert not hits, (
        "credential on a command line; pass it on stdin (curl -K -, aws --value "
        f"file:///dev/stdin) or via GIT_CONFIG_* env (infra/gerrit/lib-credentials.sh): {hits}"
    )


# ── copy-ready MCP client examples ──────────────────────────────────────────────────
# Bug limitless-defaceable-beaver: the example configs carry setup commands inside a JSON
# ``$comment`` list, where escaped quotes hide them from the line scan above.


def _example_comment_hits(text: str) -> list[str]:
    comment = json.loads(text).get("$comment", [])
    lines = comment if isinstance(comment, list) else [comment]
    return _operator_hits("\n".join(lines))


def test_the_example_scan_flags_a_header_argument_inside_a_comment():
    leaky = {
        "$comment": [
            "  claude mcp add rebar https://h/mcp/ \\",
            '    --header "Authorization: Bearer ${MCP_CLIENT_PAT_CLI}"',
        ]
    }
    assert _example_comment_hits(json.dumps(leaky)) == ["header-arg"]


def test_no_mcp_client_example_puts_a_credential_in_argv():
    examples = sorted((_REPO_ROOT / "examples" / "mcp-clients").rglob("*.json"))
    assert examples, "examples/mcp-clients/ has no JSON configs to scan"
    hits = {
        path.relative_to(_REPO_ROOT).as_posix(): found
        for path in examples
        if (found := _example_comment_hits(path.read_text(encoding="utf-8")))
    }
    assert not hits, f"credential on a command line in an example's $comment: {hits}"
