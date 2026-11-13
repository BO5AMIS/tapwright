"""Control rules and gesture classification: pure logic, fake time."""
import asyncio

import pytest
from fakes import Clock

from tapwright.control import (AGENT_MAX_WAIT_S, CALLER_LEFT, PATH_POINTS, PERSON_IDLE_S, STILL_WANTED, TAP_MS, Controller,
                               Refused, classify)

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
    asyncio.run(go())


def test_client_views_are_per_id():
    c = Controller()
    assert c.client("a") is c.client("a") and c.client("a").seen == -1 and c.client("b") is not c.client("a")


def test_a_waiting_person_goes_before_queued_agents():
    clock = Clock()

    async def go():
        c, order, queued = Controller(clock=clock, sleep=clock.sleep), [], asyncio.Event()

        async def a():
            order.append("A")
            await queued.wait()  # keep holding the lock until B and P are both waiting
            await clock.sleep(5)  # longer than PERSON_IDLE_S: P's arrival no longer counts as recent

        async def b():
            order.append("B")

        async def p():
            order.append("P")
        ta = asyncio.create_task(c.agent(a))
        await asyncio.sleep(0)
        tb = asyncio.create_task(c.agent(b))
        await asyncio.sleep(0)
        tp = asyncio.create_task(c.person(p))
        await asyncio.sleep(0)
        queued.set()
        await asyncio.gather(ta, tb, tp)
        assert order == ["A", "P", "B"]
    asyncio.run(go())


def test_pause_reaches_an_agent_that_is_waiting():
    clock = Clock()

    async def go():
        c = Controller(clock=clock, sleep=clock.sleep)
        c.person_active = lambda: True
        task = asyncio.create_task(c.agent(noop))
        for _ in range(3):
            await asyncio.sleep(0)
        c.set_paused(True)
        with pytest.raises(Refused) as e:
            await task
        assert e.value.code == "paused"
    asyncio.run(go())


def test_an_action_waiting_aside_lets_a_person_in_and_takes_its_turn_back_after_them():
    """(Codex, PR #9 cycle 1) An agent's action waiting for a person's answer lets go of the phone: a person's input runs
    meanwhile, and the action goes on after them, holding the turn again."""
    clock = Clock()

    async def go():
        c, order, answer = Controller(clock=clock, sleep=clock.sleep), [], asyncio.Event()

        async def waits():
            order.append("asked")
            await answer.wait()
            order.append("answered")

        async def action():
            assert c.holds_turn()
            await c.aside(waits)
            assert c.holds_turn() and c.driver == "agent"
            order.append("touched")
        task = asyncio.create_task(c.agent(action))
        for _ in range(3):
            await asyncio.sleep(0)
        assert order == ["asked"] and not c.holds_turn()  # (outside the action: no turn)

        async def person():
            order.append("person")
        await asyncio.wait_for(c.person(person), 1)  # not held up behind the wait
        answer.set()
        await task
        assert order == ["asked", "person", "answered", "touched"]
        assert c.generation == 2 and c.person_generation == 1
        assert await c.agent(noop) == "done"  # the lock was let go once, not twice
    asyncio.run(go())


def test_a_pause_while_an_action_waits_aside_refuses_it_when_the_answer_comes():
    async def go():
        c, answer, after = Controller(), asyncio.Event(), []

        async def action():
            await c.aside(answer.wait)
            after.append("touched")
        task = asyncio.create_task(c.agent(action))
        await asyncio.sleep(0.01)
        c.set_paused(True)
        answer.set()
        with pytest.raises(Refused) as e:
            await task
        assert e.value.code == "paused" and after == [] and c.generation == 0
        c.set_paused(False)
        assert await c.agent(noop) == "done"  # nothing is left holding the queue
    asyncio.run(go())


def test_a_refusal_during_the_wait_aside_ends_the_action_without_a_turn():
    async def go():
        c = Controller()

        async def declined():
            raise Refused("not_allowed", "they declined")

        async def action():
            await c.aside(declined)
        with pytest.raises(Refused):
            await c.agent(action)
        assert c.generation == 0 and await c.agent(noop) == "done"
        assert await c.aside(noop) == "done"  # outside an action it just waits
    asyncio.run(go())


def test_a_failed_person_action_still_counts():
    async def go():
        c = Controller()

        async def boom():
            raise RuntimeError("nope")
        with pytest.raises(RuntimeError):
            await c.person(boom)
        assert c.generation == c.person_generation == 1
    asyncio.run(go())


def test_tap_long_press_and_drag():
    tap = classify([[0, 0.5, 0.5], [80, 0.501, 0.5]], SCREEN)
    # replayed as a quick tap: the person's tap is over when it arrives, and a 60 ms hold showed 75 ms later
    assert tap.kind == "tap" and tap.points[0][1:] == (200.0, 400.0) and tap.hold_ms == TAP_MS == 20
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


def test_times_stay_in_order_and_malformed_points_are_value_errors():
    times = [p[0] for p in classify([[0, 0.1, 0.1], [50, 0.9, 0.9], [30, 0.5, 0.5]], SCREEN).points]
    assert times == sorted(times) == [0, 50, 50]
    for bad in ([[0, float("nan"), 0.5]], [[0, 0.5]], [None], [[0, float("inf"), 0.5]]):
        with pytest.raises(ValueError):
            classify(bad, SCREEN)


def test_an_action_whose_caller_left_while_it_waited_does_nothing():
    """(Tapline's Codex review, carried over) The caller's check (STILL_WANTED, set by the HTTP API) is asked as the
    turn comes: a caller that left gets `timeout` and nothing runs; the queue is let go."""
    async def go():
        c, ran, free = Controller(), [], asyncio.Event()
        holding = asyncio.create_task(c.read(free.wait))
        await asyncio.sleep(0)
        here = [True]

        async def wanted():
            return here[0]

        async def action():
            ran.append(1)
        token = STILL_WANTED.set(wanted)
        try:
            queued = asyncio.create_task(c.agent(action))
            await asyncio.sleep(0.01)
            here[0] = False  # it gave up waiting
            free.set()
            await holding
            with pytest.raises(Refused) as e:
                await queued
            assert e.value.code == "timeout" and str(e.value) == CALLER_LEFT and ran == [] and c.generation == 0
            here[0] = True
            await c.agent(action)  # a caller still there: as before, and the queue was let go
            assert ran == [1]
        finally:
            STILL_WANTED.reset(token)
        await c.agent(action)  # no check set (MCP over stdio): as before
        assert ran == [1, 1]
    asyncio.run(go())
