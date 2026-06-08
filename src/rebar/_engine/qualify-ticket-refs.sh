#!/usr/bin/env bash
set -uo pipefail
# scripts/qualify-ticket-refs.sh
# Rewrite bare `ticket <subcommand>` references in documentation to use the
# ${_PLUGIN_ROOT}/scripts/shim.
#
# Transforms:
#   ticket list       → ${_PLUGIN_ROOT}/scripts/ticket list
#   ticket show <id>  → ${_PLUGIN_ROOT}/scripts/ticket show <id>
#
# In-scope files (same as qualify-skill-refs.sh):
#   skills/, docs/, hooks/, commands/, agents/ (recursively) + CLAUDE.md
#
# NOT in scope:
#   scripts/  — internal implementation, not documentation
#   tests/    — test assertions reference the script directly
#
# Safety rules:
#   - Only rewrites known ticket subcommands (list, show, create, etc.)
#   - Skips lines that already use the shim (${_PLUGIN_ROOT}/scripts/ticket)
#   - Skips lines that use full paths (${_PLUGIN_ROOT}/scripts/ticket)
#   - Skips lines that use $CLAUDE_PLUGIN_ROOT/scripts/ticket
#   - Preserves backtick-wrapped and code-block formatting
#   - Idempotent: running twice produces the same result
#
# Usage:
#   scripts/qualify-ticket-refs.sh [--dry-run] [--verbose]
#
# Exit codes:
#   0 — Always (this is a fixer, not a checker)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# REPO_ROOT is the git repository root (not the plugin root).
# CLAUDE.md and in-scope directories are relative to REPO_ROOT.
REPO_ROOT="${PROJECT_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null)}" || {
    echo "qualify-ticket-refs: not inside a git repository" >&2
    exit 1
}
# PLUGIN_ROOT is the plugin directory (parent of scripts/).
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Parse flags ──────────────────────────────────────────────────────────────
_dry_run=false
_verbose=false
for _arg in "$@"; do
    case "$_arg" in
        --dry-run)  _dry_run=true ;;
        --verbose)  _verbose=true ;;
    esac
done

# ── Build in-scope file list ────────────────────────────────────────────────
_file_list=()
# Plugin directories (skills/, docs/, etc.) are under PLUGIN_ROOT
for _dir in skills docs hooks commands agents; do
    if [[ -d "$PLUGIN_ROOT/$_dir" ]]; then
        while IFS= read -r -d '' _f; do
            _file_list+=("$_f")
        done < <(find "$PLUGIN_ROOT/$_dir" -name '*.md' -type f -print0 2>/dev/null)
    fi
done
# CLAUDE.md is at the git repo root
if [[ -f "$REPO_ROOT/CLAUDE.md" ]]; then
    _file_list+=("$REPO_ROOT/CLAUDE.md")
fi

if [[ ${#_file_list[@]} -eq 0 ]]; then
    echo "qualify-ticket-refs: no in-scope files found" >&2
    exit 0
fi

# ── Perl rewriter ────────────────────────────────────────────────────────────
# Single perl invocation per file handles all transformations.
# Returns exit 0 if changes were made, exit 1 if no changes.
_files_changed=0
_lines_changed=0

for _file in "${_file_list[@]}"; do
    # Skip binary files
    perl -e 'exit(-T $ARGV[0] ? 0 : 1)' "$_file" 2>/dev/null || continue

    _result=$(perl -e '
use strict;
use warnings;

my $file = $ARGV[0];
my $dry_run = $ARGV[1] eq "1" ? 1 : 0;
my $verbose = $ARGV[2] eq "1" ? 1 : 0;
my $repo_root = $ARGV[3];

open(my $fh, "<", $file) or die "Cannot open $file: $!";
my @lines = <$fh>;
close($fh);

my $SHIM = "rebar";

# Known ticket subcommands
my $subcmds = "list|show|create|transition|comment|link|unlink|deps|edit|init|sync|revert|compact|fsck|bridge-status|bridge-fsck";

my $changed_lines = 0;
my @new_lines;
my @diffs;

for my $i (0 .. $#lines) {
    my $orig = $lines[$i];
    my $line = $orig;

    # NOTE: We do NOT skip entire lines that contain shim/full-path refs,
    # because a single line can contain both already-qualified and bare refs.
    # The negative lookbehinds in each regex prevent double-rewriting.

    # ── 1. Bare ticket <subcommand> → ${_PLUGIN_ROOT}/scripts/ticket <subcommand>
    # Match both backtick-wrapped and bare forms in one pass.
    # Negative lookbehind prevents double-rewriting: not preceded by "dso " or "/" or "."
    $line =~ s/(?<!dso )(?<![\/\.])(?<=`)ticket\s+($subcmds)\b/$SHIM ticket $1/g;
    $line =~ s/(?<!dso )(?<![\/\.`\w])ticket\s+($subcmds)\b/$SHIM ticket $1/g;

    if ($line ne $orig) {
        $changed_lines++;
        if ($verbose) {
            my $rel = $file;
            $rel =~ s/^\Q$repo_root\E\/?//;
            push @diffs, "  $rel:" . ($i+1) . ":\n    - " . chomp_copy($orig) . "\n    + " . chomp_copy($line);
        }
    }
    push @new_lines, $line;
}

if ($changed_lines > 0 && !$dry_run) {
    open(my $wfh, ">", $file) or die "Cannot write $file: $!";
    print $wfh @new_lines;
    close($wfh);
}

# Print diffs if verbose
for my $d (@diffs) {
    print STDERR "$d\n";
}

# Output: lines_changed
print "$changed_lines\n";

sub chomp_copy {
    my $s = $_[0];
    chomp $s;
    return $s;
}
' "$_file" "$([[ "$_dry_run" == true ]] && echo 1 || echo 0)" "$([[ "$_verbose" == true ]] && echo 1 || echo 0)" "$REPO_ROOT" 2>&1)

    # Parse result: last line is the count, everything before is verbose output
    _count=$(echo "$_result" | tail -1)
    if [[ "$_verbose" == true ]]; then
        # macOS head doesn't support -n -1; use sed to remove last line instead
        _verbose_output=$(echo "$_result" | sed '$d')
        if [[ -n "$_verbose_output" ]]; then
            echo "$_verbose_output"
        fi
    fi

    if [[ "$_count" =~ ^[0-9]+$ ]] && [[ "$_count" -gt 0 ]]; then
        _files_changed=$((_files_changed + 1))
        _lines_changed=$((_lines_changed + _count))
    fi
done

# ── Summary ──────────────────────────────────────────────────────────────────
if [[ "$_dry_run" == true ]]; then
    echo "qualify-ticket-refs: DRY RUN — would change $_lines_changed lines in $_files_changed files"
else
    echo "qualify-ticket-refs: changed $_lines_changed lines in $_files_changed files"
fi
