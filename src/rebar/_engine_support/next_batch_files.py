"""Candidate file-path extraction for ``next-batch`` (Tier C).

A self-contained heuristic unit: given a ticket's text (description + comment
bodies) and the project's configured path layout, return the set of file paths
the work is likely to touch. ``next_batch`` feeds this set into its conflict-aware
scheduler. Split out of ``next_batch`` along its natural seam — this is pure
text→paths inference with no dependency on the selection algorithm, so it stays
independently testable and re-usable. Faithful port of the bash heredoc's
``extract_files`` + the ``CFG_*`` / ``read-config.sh`` plumbing above it.
"""

from __future__ import annotations

import re

# The dispatcher passes SPRINT_KNOWN_EXTENSIONLESS_FILES="rebar" (colon-separated):
# extension-less dispatcher files the path regexes (which require an extension)
# would otherwise miss. Matched by substring so they join overlap detection.
KNOWN_EXTENSIONLESS = ("rebar",)

_AC_LINE_RE = re.compile(r"^\s*(?:AC\s+\w[\w\s]*:|Acceptance\s+criteria\s*:)", re.IGNORECASE)


def _read_config_value(key: str) -> str:
    """Read a dotted key from the flat ``.rebar/config.conf`` file, mirroring
    ``read-config.sh`` scalar mode (missing file/key → empty string)."""
    from rebar import config as _config

    path = _config.config_file()
    if path is None:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


class PathConfig:
    """Config-driven path patterns for :func:`extract_files` (the bash ``CFG_*``
    vars), plus the planning-gate flag the candidate filter consults."""

    def __init__(self) -> None:
        self.src_dir = _read_config_value("paths.src_dir") or "src"
        self.test_dir = _read_config_value("paths.test_dir") or "tests"
        self.test_unit_dir = _read_config_value("paths.test_unit_dir") or "tests/unit"
        self.extra_dir_roots = _read_config_value("paths.extra_dir_roots") or ""
        flag = _read_config_value("planning.external_dependency_block_enabled")
        self.planning_flag_enabled = flag.lower() in ("true", "1", "yes")


def extract_files(text: str, cfg: PathConfig) -> set[str]:
    """Extract candidate file paths from ticket text (faithful port of the heredoc
    ``extract_files``). Acceptance-criteria content is stripped first so validation
    commands don't create false-positive conflicts."""
    if not text:
        return set()

    # Phase 1: remove entire ## Acceptance Criteria sections (through next ## or EOF).
    text = re.sub(
        r"(?m)^##\s+ACCEPTANCE\s+CRITERIA\b.*?(?=^##\s|\Z)",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Phase 2: strip individual AC-prefixed lines.
    text = "\n".join(line for line in text.splitlines() if not _AC_LINE_RE.match(line))

    files: set[str] = set()

    # Backtick-delimited paths (any extension).
    for m in re.finditer(r"`([^`]+\.\w+)`", text):
        files.add(m.group(1).lstrip("./"))

    # Directory-rooted path regex from config values + fixed dirs.
    dir_roots = {cfg.src_dir, cfg.test_dir, "app", ".rebar", "plugins"}
    if cfg.extra_dir_roots:
        for extra in cfg.extra_dir_roots.split(","):
            extra = extra.strip()
            if extra:
                dir_roots.add(extra)
    if cfg.test_dir.endswith("s"):
        dir_roots.add(cfg.test_dir[:-1])
    dir_pattern = "|".join(re.escape(d) for d in sorted(dir_roots))

    for m in re.finditer(
        r"\b((?:" + dir_pattern + r")/[\w/\-\.]+\.(?:py|sh|md|json|yaml|toml))\b",
        text,
    ):
        files.add(m.group(1).lstrip("./"))

    # Python module notation.
    for m in re.finditer(r"\b((?:" + re.escape(cfg.src_dir) + r"|app)(?:\.\w+)+)\b", text):
        files.add(m.group(1).replace(".", "/") + ".py")

    # Known extension-less dispatcher files (matched by substring).
    for path in KNOWN_EXTENSIONLESS:
        if path and path in text:
            files.add(path)

    # Implied test files for src_dir files.
    src_prefix = cfg.src_dir + "/"
    test_unit_prefix = cfg.test_unit_dir + "/"
    implied = set()
    for f in files:
        if f.startswith(src_prefix) and f.endswith(".py"):
            inner = f[len(src_prefix) :]
            parts = inner.rsplit("/", 1)
            if len(parts) == 2:
                test_path = f"{test_unit_prefix}{parts[0]}/test_{parts[1]}"
            else:
                test_path = f"{test_unit_prefix}test_{parts[0]}"
            implied.add(test_path)
    files |= implied

    return files
