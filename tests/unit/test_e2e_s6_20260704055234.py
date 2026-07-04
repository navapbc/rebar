import pytest

from rebar._e2e_s6_base_20260704055234 import s6_target

pytestmark = pytest.mark.unit


def test_e2e_s6_semantic():
    assert s6_target() == 1
