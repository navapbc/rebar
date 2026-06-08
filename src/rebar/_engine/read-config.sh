#!/usr/bin/env bash
set -uo pipefail
# read-config.sh
# Generic config reader for rebar.
#
# Canonical config format: flat key=value (.rebar/config.conf or .rebar.conf).
# A simple-YAML fallback (.yaml/.yml) is retained for convenience.
#
# Usage (key-first):  read-config.sh [--list] [--batch] <key> [config-file]
# Usage (config-first): read-config.sh [--list] [--batch] <config-file> <key>
#
# Supports:
#   - Flat key=value format (dot-notation keys like "tickets.sync.jira_project_key")
#   - Simple YAML (nested key: value; no anchors/aliases)
#   - List mode with --list flag (returns value on one line; exit 1 if absent)
#   - Batch mode with --batch flag (outputs all keys as UPPER_CASE_WITH_UNDERSCORES=value)
#   - Missing file → empty output, exit 0
#   - Absent key in scalar mode → empty output, exit 0
#   - Absent key in --list mode → exit 1
#
# Exit codes:
#   0 — success, missing file, or missing key (scalar mode)
#   1 — missing key in --list mode (distinguishes "empty" from "absent")

list_mode=""; batch_mode=""; [[ "${1:-}" == "--list" ]] && { list_mode=1; shift; }
[[ "${1:-}" == "--batch" ]] && { batch_mode=1; shift; }

# Detect config-first form: first arg contains '/' or ends with .conf/.yaml/.yml
arg1="${1:-}"
if [[ "$arg1" == *"/"* || "$arg1" == *.conf || "$arg1" == *.yaml || "$arg1" == *.yml ]]; then
    config_file="$arg1"; key="${2:-}"
else
    key="$arg1"; config_file="${2:-}"
fi

# Resolve config file when not specified.
# Resolution order:
#   1. WORKFLOW_CONFIG_FILE / REBAR_CONFIG env var (exact path — for test isolation)
#   2. <repo-root>/.rebar/config.conf, then <repo-root>/.rebar.conf
# Repo-root honors REBAR_ROOT / PROJECT_ROOT, falling back to git toplevel.
if [[ -z "$config_file" ]]; then
    if [[ -n "${WORKFLOW_CONFIG_FILE:-}" ]]; then
        config_file="${WORKFLOW_CONFIG_FILE}"
    elif [[ -n "${REBAR_CONFIG:-}" ]]; then
        config_file="${REBAR_CONFIG}"
    else
        _root="${REBAR_ROOT:-${PROJECT_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || echo "")}}"
        if [[ -n "$_root" && -f "$_root/.rebar/config.conf" ]]; then
            config_file="$_root/.rebar/config.conf"
        elif [[ -n "$_root" && -f "$_root/.rebar.conf" ]]; then
            config_file="$_root/.rebar.conf"
        else
            exit 0
        fi
    fi
fi
# Missing file: exit 0 (graceful degradation)
if [[ ! -f "$config_file" ]]; then
    exit 0
fi

# ── Detect file format ────────────────────────────────────────────────────────
# YAML files (.yaml/.yml) or files whose first non-comment line contains ':'
# but not '=' are parsed with Python; otherwise flat KEY=VALUE.
_is_yaml() {
    if [[ "$config_file" == *.yaml || "$config_file" == *.yml ]]; then
        return 0
    fi
    local first_line
    first_line=$(grep -v '^\s*#' "$config_file" | grep -v '^\s*$' | head -1)
    if [[ "$first_line" == *":"* && "$first_line" != *"="* ]]; then
        return 0
    fi
    return 1
}

# ── YAML reader (pure Python, no pyyaml dependency) ─────────────────────────
_yaml_read_key() {
    local file="$1" dotkey="$2"
    python3 -c "
import sys, re

def parse_simple_yaml(filepath):
    result = {}
    stack = [(-1, result)]
    with open(filepath) as f:
        for line in f:
            stripped = line.rstrip()
            if not stripped or stripped.lstrip().startswith('#'):
                continue
            indent = len(line) - len(line.lstrip())
            while len(stack) > 1 and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1]
            m = re.match(r'^(\s*)([^#:]+?):\s*(.*)', stripped)
            if not m:
                continue
            key = m.group(2).strip()
            value = m.group(3).strip()
            if value:
                if (value.startswith('\"') and value.endswith('\"')) or \
                   (value.startswith(\"'\") and value.endswith(\"'\")):
                    value = value[1:-1]
                if value.lower() in ('true', 'yes'):
                    parent[key] = True
                elif value.lower() in ('false', 'no'):
                    parent[key] = False
                else:
                    parent[key] = value
            else:
                child = {}
                parent[key] = child
                stack.append((indent, child))
    return result

data = parse_simple_yaml(sys.argv[1])
keys = sys.argv[2].split('.')
val = data
for k in keys:
    if isinstance(val, dict) and k in val:
        val = val[k]
    else:
        sys.exit(2)

if isinstance(val, bool):
    print(str(val))
elif val is not None:
    print(str(val))
" "$file" "$dotkey"
}

_yaml_batch() {
    local file="$1"
    python3 -c "
import sys, re

def parse_simple_yaml(filepath):
    result = {}
    stack = [(-1, result)]
    with open(filepath) as f:
        for line in f:
            stripped = line.rstrip()
            if not stripped or stripped.lstrip().startswith('#'):
                continue
            indent = len(line) - len(line.lstrip())
            while len(stack) > 1 and stack[-1][0] >= indent:
                stack.pop()
            parent = stack[-1][1]
            m = re.match(r'^(\s*)([^#:]+?):\s*(.*)', stripped)
            if not m:
                continue
            key = m.group(2).strip()
            value = m.group(3).strip()
            if value:
                if (value.startswith('\"') and value.endswith('\"')) or \
                   (value.startswith(\"'\") and value.endswith(\"'\")):
                    value = value[1:-1]
                if value.lower() in ('true', 'yes'):
                    parent[key] = True
                elif value.lower() in ('false', 'no'):
                    parent[key] = False
                else:
                    parent[key] = value
            else:
                child = {}
                parent[key] = child
                stack.append((indent, child))
    return result

def flatten(d, prefix=''):
    for k, v in d.items():
        full_key = f'{prefix}.{k}' if prefix else k
        if isinstance(v, dict):
            flatten(v, full_key)
        else:
            var_name = full_key.upper().replace('.', '_')
            val = str(v) if v is not None else ''
            safe_val = val.replace(\"'\", \"'\\\\''\" )
            print(f\"{var_name}='{safe_val}'\")

flatten(parse_simple_yaml(sys.argv[1]))
" "$file"
}

if _is_yaml; then
    if [[ -n "$batch_mode" ]]; then
        _yaml_batch "$config_file"
        exit 0
    elif [[ -n "$list_mode" ]]; then
        result=$(_yaml_read_key "$config_file" "$key") || exit 1
        [[ -n "$result" ]] && { printf '%s\n' "$result"; exit 0; }
        exit 1
    else
        result=$(_yaml_read_key "$config_file" "$key" 2>/dev/null) || true
        printf '%s' "$result"
        exit 0
    fi
fi

# ── .conf format: flat KEY=VALUE lines ───────────────────────────────────────
_conf_lines() { grep -v '^\s*#' "$config_file"; }
if [[ -n "$batch_mode" ]]; then
    while read -r line; do
        [[ -z "$line" ]] && continue
        raw_key="${line%%=*}"
        raw_val="${line#*=}"
        var_name="${raw_key^^}"        # uppercase
        var_name="${var_name//./_}"    # dots to underscores
        safe_val="${raw_val//\'/\'\\\'\'}"
        printf "%s='%s'\n" "$var_name" "$safe_val"
    done < <(_conf_lines | grep -E '^[^=]+=')
    exit 0
elif [[ -n "$list_mode" ]]; then
    results=$(_conf_lines | grep "^${key}=" | cut -d= -f2-)
    [[ -n "$results" ]] && { printf '%s\n' "$results"; exit 0; }; exit 1
else
    printf '%s' "$(_conf_lines | grep -m1 "^${key}=" | cut -d= -f2-)"; exit 0
fi
