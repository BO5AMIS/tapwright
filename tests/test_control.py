"""Control rules and gesture classification: pure logic, fake time."""
import asyncio

import pytest
from fakes import Clock

from tapwright.control import (AGENT_MAX_WAIT_S, PATH_POINTS, PERSON_IDLE_S, Controller, Refused, classify)

SCREEN = (400.0, 800.0)


async def noop():
    return "done"


def test_one_action_at_a_time_and_reads_dont_count():
    async def go():
        c, order = Controller(), []

        async def slow(tag):
            order.append(f"start {tag}")
            await asyncio.sleep(0.01)
            order.append(f"end {tag}")
        await asyncio.gather(c.agent(lambda: slow("a")), c.read(lambda: slow("b")))
        assert order == ["start a", "end a", "start b", "end b"]
        assert c.generation == 1  # only the action
    asyncio.run(go())


def test_agent_waits_for_a_person_to_go_idle():
    clock = Clock()

    async def go():
        c = Controller(clock=clock, sleep=clock.sleep)
        await c.person(noop)
        assert c.driver == "person" and c.person_generation == c.generation == 1
        start = clock.t
        assert await c.agent(noop) == "done"
        assert clock.t - start >= PERSON_IDLE_S and c.driver == "agent"
    asyncio.run(go())


def test_agent_gives_up_while_a_person_keeps_driving():
    clock = Clock()

    async def go():
        c = Controller(clock=clock, sleep=clock.sleep)
        c.person_active = lambda: True
        with pytest.raises(Refused) as e:
            await c.agent(noop)
        assert e.value.code == "busy" and clock.t - 1000.0 >= AGENT_MAX_WAIT_S
    asyncio.run(go())


def test_pause_refuses_agents_not_people():
    async def go():
        c = Controller()
        c.set_paused(True)
        with pytest.raises(Refused) as e:
            await c.agent(noop)
        assert e.value.code == "paused"
        assert await c.person(noop) == "done"
        c.set_paused(False)
        assert await c.agent(noop) == "done"
    asyncio.run(go())


def test_a_refused_action_leaves_the_screen_generation_alone():
    async def go():
        c = Controller()

        async def refuse():
            raise Refused("stale", "look again")
        with pytest.raises(Refused):
            await c.agent(refuse)
        assert c.generation == 0
        assert c.client("a").seen == -1 and c.client("a") is c.client("a")
    asyncio.run(go())


def test_tap_long_press_and_drag():
    tap = classify([[0, 0.5, 0.5], [80, 0.501, 0.5]], SCREEN)
    assert tap.kind == "tap" and tap.points[0][1:] == (200.0, 400.0) and tap.hold_ms == 60
    hold = classify([[0, 0.5, 0.5], [600, 0.5, 0.505]], SCREEN)
    assert hold.kind == "longPress" and hold.hold_ms == 600
    drag = classify([[i * 10, 0.5, 0.9 - i * 0.01] for i in range(60)], SCREEN)
    assert drag.kind == "drag" and len(drag.points) == PATH_POINTS
    assert drag.points[0] == (0, 200.0, 720.0) and drag.points[-1][0] == 590


def test_points_are_clamped_and_bad_paths_rejected():
    assert classify([[5, -0.2, 1.4]], SCREEN).points[0] == (0, 0.0, 800.0)  # time starts at 0
    with pytest.raises(ValueError):
        classify([], SCREEN)
    with pytest.raises(ValueError):
        classify([[0, "a"]], SCREEN)
