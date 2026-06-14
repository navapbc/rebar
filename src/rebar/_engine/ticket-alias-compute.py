#!/usr/bin/env python3
"""Compute a human-readable alias from a ticket ID and a wordlist file.

Usage:
    ticket-alias-compute.py <ticket_id> <wordlist_path>

Outputs:
    The alias string (e.g. "bold-swift-dawn") to stdout.
    If the wordlist is missing or empty, prints the first 8 hex chars as fallback
    and writes "FALLBACK" to stderr, then exits 0.

Exit codes:
    0  success (including fallback)
    1  usage error (wrong number of arguments)
"""

import sys


def load_words(path):
    adjs, nouns = [], []
    section = "adj"
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line == "# NOUNS":
                    section = "noun"
                    continue
                if line.startswith("#") or not line.strip():
                    continue
                if section == "adj":
                    adjs.append(line.strip())
                else:
                    nouns.append(line.strip())
    except OSError:
        # Intentional: wordlist is optional. Caller checks for empty lists and
        # falls back to the hex-alias path (main() prints hex_id[:8] + FALLBACK).
        pass
    return adjs, nouns


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <ticket_id> <wordlist_path>", file=sys.stderr)
        sys.exit(1)

    ticket_id = sys.argv[1]
    wordlist_path = sys.argv[2]
    hex_id = ticket_id.replace("-", "")

    adjs, nouns = load_words(wordlist_path)

    if len(adjs) == 0 or len(nouns) == 0:
        # Fallback: first 8 hex chars, no dash
        print(hex_id[:8])
        print("FALLBACK", file=sys.stderr)
        sys.exit(0)

    adj = adjs[int(hex_id[0:4], 16) % len(adjs)]
    noun1 = nouns[int(hex_id[4:8], 16) % len(nouns)]
    # Legacy 8-hex IDs (xxxx-xxxx) have no hex[8:12]; emit a 2-word alias
    # rather than crashing on int("", 16). Matches ticket_reducer._alias
    # so the same ticket_id yields the same alias on both code paths.
    if len(hex_id) >= 12:
        noun2 = nouns[int(hex_id[8:12], 16) % len(nouns)]
        print(f"{adj}-{noun1}-{noun2}")
    else:
        print(f"{adj}-{noun1}")


if __name__ == "__main__":
    main()
