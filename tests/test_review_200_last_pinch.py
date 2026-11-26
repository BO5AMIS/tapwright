"""Reviewer N: pinch_spread (e321ccd) refuses a documented scale (0.2 to 5, not 1) at the middle of the screen with
"too near the screen's edge"."""
import pytest

from tapwright.control import Refused
from tapwright.session import pinch_spread

SCREEN = (390.0, 844.0)


@pytest.mark.parametrize("scale", [1.1, 0.9, 1.12])
def test_a_small_documented_scale_in_the_middle_of_the_screen_is_not_refused_as_near_an_edge(scale):
    try:
        near, far = pinch_spread(195.0, 422.0, scale, SCREEN)
    except Refused as e:
        pytest.fail(f"scale {scale} at the centre refused: {e}")
    assert near != far
