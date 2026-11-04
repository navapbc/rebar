"""First-run seeding of deployed-sha from the legacy /opt/rebar/.deployed_ref marker.

When /var/lib/rebar/deployed-sha is empty, autodeploy adopts a deployed SHA without deploying.
The legacy hand-deploy marker is honoured only when it names a commit on main; an off-main ref
(the live marker names a July feature-branch commit the mirror still resolves) must not become
the deployed state, because every later component delta would be computed from it.

The script runs for real against a throwaway git mirror. A ``git`` wrapper fakes only the two
network-facing calls (``remote get-url`` and ``fetch``) and hands everything else to real git,
so ancestry is decided by git itself rather than by a stub.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

AUTODEPLOY = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "autodeploy.sh"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, **_GIT_ENV, "HOME": str(repo.parent)}
    out = subprocess.run(
        ["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name + "\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def first_run_box(tmp_path: Path) -> dict[str, object]:
    """A box with an empty deployed-sha and a mirror holding main (A -> B) plus off-main S."""
    real_git = shutil.which("git")
    assert real_git, "git is required"
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    _git(mirror, "init", "-q", "-b", "main")
    main_a = _commit(mirror, "a")
    main_b = _commit(mirror, "b")
    _git(mirror, "checkout", "-q", "-b", "side", main_a)
    side_s = _commit(mirror, "s")
    _git(mirror, "checkout", "-q", "main")
    _git(mirror, "update-ref", "refs/remotes/origin/main", main_b)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        + textwrap.dedent(
            f"""\
            case " $* " in
              *" remote get-url "*) echo "https://github.com/navapbc/rebar.git"; exit 0 ;;
              *" fetch "*) exit 0 ;;
            esac
            exec "{real_git}" "$@"
            """
        )
    )
    wrapper.chmod(0o755)
    # flock/timeout are GNU-only; macOS runners lack them. The container tools are inert so a
    # tick that gets past the seed never touches the host's real docker daemon.
    (bin_dir / "timeout").write_text('#!/usr/bin/env bash\nshift; exec "$@"\n')
    for tool in ("flock", "docker", "curl", "rsync", "systemctl"):
        (bin_dir / tool).write_text("#!/usr/bin/env bash\nexit 0\n")
    for tool in ("flock", "timeout", "docker", "curl", "rsync", "systemctl"):
        (bin_dir / tool).chmod(0o755)

    state = tmp_path / "state"
    state.mkdir()
    deploy_repo = tmp_path / "deploy"
    deploy_repo.mkdir()
    env = {
        **_GIT_ENV,
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_DIR": str(state),
        "DEPLOY_REPO": str(deploy_repo),
        "COMPOSE_DIR": str(deploy_repo / "infra" / "compose"),
        "MIRROR_DIR": str(mirror),
    }
    return {
        "env": env,
        "state": state,
        "deploy_repo": deploy_repo,
        "main_a": main_a,
        "main_b": main_b,
        "side_s": side_s,
    }


def _run(
    box: dict[str, object], marker: str | None
) -> tuple[subprocess.CompletedProcess[str], str]:
    deploy_repo = box["deploy_repo"]
    assert isinstance(deploy_repo, Path)
    if marker is not None:
        (deploy_repo / ".deployed_ref").write_text(marker + "\n")
    result = subprocess.run(
        ["bash", str(AUTODEPLOY)],
        env=box["env"],  # type: ignore[arg-type]
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    state = box["state"]
    assert isinstance(state, Path)
    sha_file = state / "deployed-sha"
    seeded = sha_file.read_text().strip() if sha_file.exists() else ""
    return result, seeded


def _errors(result: subprocess.CompletedProcess[str]) -> list[dict[str, str]]:
    found = re.findall(r"^AUTODEPLOY_ERROR (\{.*\})$", result.stderr, re.MULTILINE)
    return [json.loads(m) for m in found]


def _context(result: subprocess.CompletedProcess[str]) -> str:
    return f"rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_off_main_marker_is_refused_and_tip_adopted(first_run_box: dict[str, object]) -> None:
    side = str(first_run_box["side_s"])
    result, seeded = _run(first_run_box, f"{side[:9]} (feat/some-branch)")
    assert result.returncode == 0, _context(result)
    assert seeded == first_run_box["main_b"], (
        "an off-main .deployed_ref must not become the deployed state; the first run must "
        f"adopt the main tip instead, got {seeded!r}.\n{_context(result)}"
    )
    off_main = [e for e in _errors(result) if e["reason"] == "deployed-ref-off-main"]
    assert off_main, f"the rejected marker must be signalled.\n{_context(result)}"
    assert side[:9] in off_main[0]["detail"], off_main[0]


def test_main_ancestor_marker_is_still_adopted(first_run_box: dict[str, object]) -> None:
    main_a = str(first_run_box["main_a"])
    result, seeded = _run(first_run_box, f"{main_a[:9]} (main)")
    assert result.returncode == 0, _context(result)
    assert seeded == main_a, (
        "a hand-deploy marker on main must still seed deployed-sha so the first tick deploys "
        f"the real delta, got {seeded!r}.\n{_context(result)}"
    )
    assert not _errors(result), _context(result)


@pytest.mark.parametrize("marker", [None, "0123456789abcdef (gone)", ""])
def test_missing_or_unresolvable_marker_adopts_tip(
    first_run_box: dict[str, object], marker: str | None
) -> None:
    result, seeded = _run(first_run_box, marker)
    assert result.returncode == 0, _context(result)
    assert seeded == first_run_box["main_b"], _context(result)
    assert not _errors(result), _context(result)


def test_existing_deployed_sha_ignores_marker(first_run_box: dict[str, object]) -> None:
    """Steady state never reads the marker: a populated deployed-sha is left as it is."""
    state = first_run_box["state"]
    assert isinstance(state, Path)
    main_b = str(first_run_box["main_b"])
    (state / "deployed-sha").write_text(main_b + "\n")
    # Pre-populate the component markers too, so the tick reaches its up-to-date exit before
    # any docker work.
    (state / "mcp-deployed-sha").write_text(main_b + "\n")
    (state / "bot-deployed-sha").write_text(main_b + "\n")
    result, seeded = _run(first_run_box, f"{str(first_run_box['side_s'])[:9]} (feat/x)")
    assert seeded == main_b, _context(result)
    assert "first run" not in result.stdout, _context(result)
