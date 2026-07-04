"""e2e S6 caller — imports s6_target (which main will delete)."""

from rebar._e2e_s6_base_20260704045044 import s6_target


def s6_call() -> int:
    return s6_target()
