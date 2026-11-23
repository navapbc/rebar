# shellcheck shell=bash
# Sourced by the infra/gerrit operator scripts. Hands a credential to curl and git
# without putting it on either command line (bug immoral-heated-doctorfish): a
# process's argv is readable by every local user through ps and /proc/<pid>/cmdline
# for as long as it runs. printf is a shell builtin, so `printf ... | cmd` moves the
# value through a pipe without starting any process that carries it in argv.
# Never echo the credential; callers keep `set -x` off while these run.

# Escape a value for a double-quoted curl config-file string (backslash, then quote).
_curl_cfg_escape() {
	local v=$1
	v=${v//\\/\\\\}
	v=${v//\"/\\\"}
	printf '%s' "$v"
}

# curl_auth <user> <password> <curl args...>
# Basic auth handed to curl as a config file on stdin (`-K -`), not as `-u user:pw`.
curl_auth() {
	local user pw
	user="$(_curl_cfg_escape "$1")"
	pw="$(_curl_cfg_escape "$2")"
	shift 2
	printf 'user = "%s:%s"\n' "$user" "$pw" | curl -K - "$@"
}

# git_auth <base-url> <user> <password> <git args...>
# Run git with a Basic header scoped to <base-url>, carried in git's own
# GIT_CONFIG_COUNT/KEY_<n>/VALUE_<n> environment (git >= 2.31) after any inherited
# entries, instead of a user:pw@host URL. credential.helper is reset to empty so the
# operator's own helper (keychain) is neither asked nor told to store or erase.
git_auth() {
	local base=${1%/} basic n
	basic="$(printf '%s:%s' "$2" "$3" | base64 | tr -d '\n')"
	shift 3
	n=${GIT_CONFIG_COUNT:-0}
	case $n in '' | *[!0-9]*) n=0 ;; esac
	(
		export "GIT_CONFIG_KEY_${n}=credential.helper" "GIT_CONFIG_VALUE_${n}="
		export "GIT_CONFIG_KEY_$((n + 1))=http.${base}/.extraHeader"
		export "GIT_CONFIG_VALUE_$((n + 1))=Authorization: Basic ${basic}"
		export GIT_CONFIG_COUNT=$((n + 2)) GIT_TERMINAL_PROMPT=0
		git "$@"
	)
}
