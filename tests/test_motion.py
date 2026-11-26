"""motion.py (2.0, SPEC D6): human-like paths, deterministic with a seeded random.Random."""
import math
import random

import pytest

from tapwright import motion, ui


@pytest.mark.parametrize("seed", range(20))
def test_a_swipe_rests_moves_on_time_and_ends_exactly_at_its_target(seed):
    rng = random.Random(seed)
    points = motion.swipe(380, 520, 60, 540, 300, rng, human=True)
    times = [t for t, _, _ in points]
    assert times[0] == 0 and all(b > a for a, b in zip(times, times[1:]))
    assert points[0][1:] == (380, 520) and points[-1][1:] == (60, 540)
    dwell = points[1][0]
    assert 20 <= dwell <= 40 and points[1][1:] == (380, 520)  # pressed, still, before it moves
    assert times[-1] == pytest.approx(dwell + 300)
    assert all(times[i + 1] - times[i] <= motion.STEP_MS + 2 * motion.JITTER_MS + 1 for i in range(1, len(times) - 1))


@pytest.mark.parametrize("seed", range(20))
def test_a_swipe_bows_a_little_off_the_line(seed):
    points = motion.swipe(100, 800, 100, 200, 350, random.Random(seed), human=True)
    off = motion.deviation(points)
    assert 0.5 < off <= motion.ARC_MAX_PT + 1e-9


def test_the_arc_is_small_for_a_short_swipe():
    points = motion.swipe(100, 100, 140, 100, 200, random.Random(1), human=True)
    assert motion.deviation(points) <= 40 * motion.ARC_SHARE[1] + 1e-9


def test_the_speed_profile_is_slow_fast_slow():
    points = motion.swipe(0, 0, 600, 0, 600, random.Random(3), human=True)[1:]  # from the end of the rest
    steps = [math.hypot(b[1] - a[1], b[2] - a[2]) / (b[0] - a[0]) for a, b in zip(points, points[1:])]
    middle = max(steps)
    assert steps[0] < middle / 3 and steps[-1] < middle / 3


def test_same_seed_same_path_and_different_seeds_differ():
    a = motion.swipe(10, 10, 300, 600, 300, random.Random(42), human=True)
    b = motion.swipe(10, 10, 300, 600, 300, random.Random(42), human=True)
    c = motion.swipe(10, 10, 300, 600, 300, random.Random(43), human=True)
    assert a == b and a != c


def test_a_tap_lands_within_a_point_and_a_half_and_holds_like_a_person():
    rng = random.Random(5)
    for _ in range(500):
        x, y, hold = motion.tap(200, 300, rng, human=True)
        assert math.hypot(x - 200, y - 300) <= motion.TAP_OFF_MAX
        assert 60 <= hold <= 110
        assert 60 <= motion.tap_hold(rng) <= 110


def test_humans_off_gives_todays_straight_paths():
    assert motion.swipe(1, 2, 3, 4, 350, random.Random(0), human=False) == [
        (t, x, y) for t, x, y in ui.swipe_points(1, 2, 3, 4, 350)]
    assert motion.tap(5, 6, random.Random(0), human=False) == (5, 6, 0)
    assert motion.SWIPE_PRESS_MS == ui.SWIPE_PRESS_MS


def test_a_swipe_that_does_not_move_still_ends_on_time():
    points = motion.swipe(50, 50, 50, 50, 200, random.Random(0), human=True)
    assert points[-1] == (points[1][0] + 200, 50, 50)


def test_min_jerk_runs_from_nothing_to_all_and_is_half_way_at_half_time():
    assert motion.min_jerk(0) == 0 and motion.min_jerk(1) == 1 and motion.min_jerk(0.5) == pytest.approx(0.5)
    values = [motion.min_jerk(i / 50) for i in range(51)]
    assert all(b >= a for a, b in zip(values, values[1:]))

@pytest.mark.parametrize("ms", [0.0, 1.0, 1.4, 2.0, 3.0])
def test_a_very_short_swipe_keeps_every_sample_after_the_rest_and_in_order(ms):
    # (CodeRabbit, PR #26) a 1 ms move got 4 intervals; the room kept for the samples after one put it before the rest
    for seed in range(20):
        points = motion.swipe(0, 0, 100, 0, ms, random.Random(seed), human=True)
        times = [t for t, _, _ in points]
        assert all(b > a for a, b in zip(times[1:], times[2:])), (ms, seed, times)  # (after the rest: strictly later)
        assert times[1] <= min(times[2:])
