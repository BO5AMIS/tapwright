"""Tapwright 2.0.3, round G2 (phone 2026-11-27): a product page of a shopping app never reads (the app's own timeouts),
and each long read of it blocked the whole app (60, 120, 240, 480 s planned): the app's readable pages, the home screen
after a relaunch too, answered "0 elements" for minutes; once the block ran out the home screen read whole in 2.5-5.3 s.
A long read that wasn't whole now blocks the next one only on the screen it failed on, until the third in a row: then
the app's, as before (review R203c H1)."""
from test_slow_readable_203 import CUT, SHOP, WHOLE, ShopRunner, Stream, budget, look, run, shop, state  # noqa: F401

from tapwright import session as session_module

PAGE, HOME = b"product page", b"home screen"  # (byte frames: alike only to themselves, so two screens differ wholly)


def _on(stream: Stream, picture: bytes) -> None:
    """The phone shows `picture`, before and after each read."""
    stream.picture = stream.after = picture


def _shop(stream: Stream, runner: ShopRunner):
    s, runner = shop(stream, runner)
    s.other_screen = stream.d._other_screen  # (as the daemon wires it)
    return s, runner


# ---- the phone's sequence ----

def test_a_page_that_never_reads_blocks_only_itself_and_another_page_reads_with_the_long_try(state):
    async def go():
        stream = Stream()
        s, runner = _shop(stream, ShopRunner(stream, answers={}))  # the product page: nothing reads in time
        _on(stream, PAGE)
        await look(s, stream)
        assert runner.budgets() == [3000, 8000] and s.budgets.blocked(SHOP)
        stream.now[0] += 20.0
        await look(s, stream)  # the same page, inside its block: no long try
        assert runner.budgets()[2:] == [3000]
        runner.answers = {3000: CUT, 8000: WHOLE}  # the home screen, which reads with the long budget
        _on(stream, HOME)
        stream.now[0] += 20.0
        tree = await look(s, stream)
        assert runner.budgets()[3:] == [3000, 8000] and tree.tier == "full"  # (blocked for the app before round G2)
        assert not s.budgets.blocked(SHOP) and s.budgets.slow(SHOP)  # (a whole long read ends the block)
    run(go)


def test_a_readable_apps_planned_long_read_goes_on_another_screen_inside_the_block(state):
    async def go():
        stream = Stream()
        s, runner = _shop(stream, ShopRunner(stream))
        _on(stream, HOME)
        await look(s, stream)  # the home screen reads whole with the long try: slow but readable
        runner.answers = {}
        _on(stream, PAGE)
        stream.now[0] += 1.0
        await look(s, stream)  # the product page: its planned long read is cut
        assert runner.budgets() == [3000, 8000, 8000] and s.budgets.blocked(SHOP)
        stream.now[0] += 10.0
        await look(s, stream)  # the same page: 2.0.2's rules, no 8 s
        assert 8000 not in runner.budgets()[3:]
        runner.answers = {3000: CUT, 8000: WHOLE}
        _on(stream, HOME)
        stream.now[0] += 10.0
        await look(s, stream)  # back home inside the block: planned with the long budget at once
        assert runner.budgets()[-1] == 8000 and s.last_tier == "full"
    run(go)


def test_from_the_third_failure_in_a_row_the_block_is_the_apps_again(state):
    async def go():
        stream = Stream()
        s, runner = _shop(stream, ShopRunner(stream, answers={}))
        for n in range(3):  # three different-looking pages, each long read cut
            _on(stream, b"page %d" % n)
            await look(s, stream)
            stream.now[0] += 5.0
        assert runner.budgets().count(8000) == 3
        assert s.budgets.misses(SHOP) == session_module.LONG_APP_MISSES
        _on(stream, b"page 4")
        await look(s, stream)  # a fourth page: blocked for the app's span, with today's doubling
        assert runner.budgets().count(8000) == 3 and s.budgets.block_state(SHOP) == "app"
        assert s.budgets.block_span(SHOP) == 4 * session_module.LONG_MEMORY
    run(go)


def test_with_no_picture_to_compare_the_block_holds_as_before(state):
    async def go():
        stream = Stream()
        s, runner = shop(stream, ShopRunner(stream, answers={}))  # (no other_screen: a session without the daemon's)
        _on(stream, PAGE)
        await look(s, stream)
        runner.answers = {3000: CUT, 8000: WHOLE}
        _on(stream, HOME)
        stream.now[0] += 20.0
        await look(s, stream)
        assert runner.budgets() == [3000, 8000, 3000] and s.budgets.block_state(SHOP) == "unseen"
    run(go)


def test_a_playing_feed_gets_no_long_try_from_another_screen_either(state):
    async def go():
        stream = Stream()
        s, runner = _shop(stream, ShopRunner(stream, answers={}))
        _on(stream, PAGE)
        await look(s, stream)
        stream.now[0] += 20.0
        stream.after = b"next video"  # the picture changes across the read: a feed
        await look(s, stream, moving=True)
        assert runner.budgets() == [3000, 8000, 1500]  # (moving: 2.0.2's short budget, and no long try)
    run(go)


# ---- the rules ----

def test_block_state_by_the_picture_and_the_count():
    clock = [100.0]
    b = budget(clock=lambda: clock[0])
    other = {"now": False}
    b.other = lambda picture: other["now"]
    b.long_sent(SHOP, "page")
    b.answered(SHOP, True, 8000)
    assert b.block_state(SHOP) == "same" and b.blocked(SHOP)
    other["now"] = True
    assert b.block_state(SHOP) == "other" and not b.blocked(SHOP) and b.long_try(SHOP)
    b.long_sent(SHOP, "page 2")
    b.answered(SHOP, True, 8000)
    assert b.block_state(SHOP) == "other"  # (two in a row: still the screen's)
    b.long_sent(SHOP, "page 3")
    b.answered(SHOP, True, 8000)
    assert b.block_state(SHOP) == "app" and b.blocked(SHOP)  # (the third: the app's)
    clock[0] += b.block_span(SHOP)
    assert b.block_state(SHOP) == "" and not b.blocked(SHOP)
    b.long_sent(SHOP, None)
    b.answered(SHOP, True, 8000)
    assert b.block_state(SHOP) == "app"  # (the count stays the app's)
    b.long_sent(SHOP, "home")
    b.answered(SHOP, False, 8000)  # whole: everything back to nothing
    assert b.block_state(SHOP) == "" and b.misses(SHOP) == 1


def test_a_long_read_answered_by_another_apps_tree_puts_the_picture_back():
    b = budget(clock=lambda: 100.0)
    b.other = lambda picture: picture == "page"  # (the screen now is another than "page", not than "home")
    b.long_sent(SHOP, "page")
    b.answered(SHOP, True, 8000)
    assert b.block_state(SHOP) == "other"
    b.long_sent(SHOP, "home")
    b.answered("com.apple.springboard", False, 8000, sent_for=SHOP)  # (a banner's tree answered it)
    assert b.block_state(SHOP) == "other" and b.misses(SHOP) == 1


def test_no_picture_or_no_comparison_is_blocked():
    b = budget(clock=lambda: 100.0)
    b.long_sent(SHOP, "page")
    b.answered(SHOP, True, 8000)
    assert b.block_state(SHOP) == "unseen"  # (no `other`)

    def broken(picture):
        raise ValueError("can't compare")
    b.other = broken
    assert b.block_state(SHOP) == "unseen"


def test_the_daemons_comparison_takes_a_quarter_of_the_picture_for_another_screen():
    from tapwright import daemon as daemon_module
    from tapwright.daemon import StillMark
    assert daemon_module.OTHER_SCREEN == 0.25
    stream = Stream()
    d = stream.d
    stream.idle(0.2)
    kept = StillMark(d.hub.latest)
    assert not d._other_screen(kept)  # the same picture
    stream.picture = b"another"
    stream.idle(0.2)
    assert d._other_screen(kept)
    assert not d._other_screen(StillMark(None)) and not d._other_screen(None)


# ---- round G: a long read that gave elements, only not all, is no miss ----

def test_a_shallow_long_read_of_a_readable_app_doesnt_blind_the_next_read(state):
    from test_slow_readable_203 import ITEM
    from test_vision_200 import _app

    from tapwright import ui
    shallow = {"token": "s", "tree": _app(SHOP, "Shop", [ITEM]), "tier": "shallow", "truncated": True,
               "why": "partly read"}

    async def go():
        stream = Stream()
        s, runner = _shop(stream, ShopRunner(stream))
        _on(stream, HOME)
        await look(s, stream)  # open_app: the 3 s read cut, the long one whole: slow but readable
        runner.answers = {3000: CUT, 8000: shallow}
        stream.now[0] += 3.0
        await look(s, stream)  # the planned long read comes back partly read
        assert runner.budgets() == [3000, 8000, 8000]
        assert not s.budgets.blocked(SHOP) and SHOP not in s.budgets._long_misses
        runner.answers = {3000: CUT, 8000: WHOLE}
        stream.now[0] += 8.0
        await look(s, stream)  # the tap's own read: the long budget again, not blocked
        assert runner.budgets()[-1] == 8000 and s.last_tier == "full"
        assert ui.resolve(s.items, ui.Target(id="deal-1")).label == "Deal of the day"
    run(go)


def test_a_shallow_long_read_neither_starts_nor_lifts_a_block_nor_moves_the_count():
    clock = [100.0]
    b = budget(clock=lambda: clock[0])
    b.long_sent(SHOP, "page")
    b.answered(SHOP, True, 8000, partial=True)
    assert not b.blocked(SHOP) and SHOP not in b._long_misses
    b.long_sent(SHOP, "page")
    b.answered(SHOP, True, 8000)  # a root: a miss
    cut_at = b._long_cut[SHOP]
    clock[0] += 10.0
    b.long_sent(SHOP, "page")
    b.answered(SHOP, True, 8000, partial=True)
    assert b._long_cut[SHOP] == cut_at and b.misses(SHOP) == 1 and b.block_state(SHOP) == "unseen"


# ---- round G: a read is planned by the app in front ----

def test_open_app_from_a_slow_app_plans_the_next_read_by_the_app_now_in_front(state):
    from test_slow_readable_203 import OTHER
    from test_vision_200 import _app
    other_whole = {"token": "o", "tree": _app(OTHER, "Other", []), "tier": "full", "truncated": False}

    class FrontRunner(ShopRunner):
        front = SHOP

        async def http(self, method, path, payload=None, timeout=30.0):
            if path.replace("/session/S", "").startswith("/wda/tw/front"):
                self.requests.append((method, path, payload))
                return {"value": {"b": self.front, "pid": 9, "ms": 4}}
            return await super().http(method, path, payload, timeout)

    async def go():
        stream = Stream()
        s, runner = _shop(stream, FrontRunner(stream, version=11))
        _on(stream, HOME)
        await look(s, stream)  # the slow app, read whole with the long budget
        assert s.budgets.slow(SHOP) and runner.budgets() == [3000, 8000]
        runner.front = OTHER  # open_app of another app
        # (the slow app's long read, had it been sent, finds it gone: a root of it, as on the phone)
        runner.answers = {3000: other_whole, 8000: CUT}
        stream.now[0] += 1.0
        await s.front_app()  # (the daemon asks it right before the answer's read)
        await look(s, stream)
        assert runner.budgets()[2:] == [3000]  # the new app's own budget, no 8 s
        assert not s.budgets.blocked(SHOP) and SHOP not in s.budgets._long_misses and s.budgets.slow(SHOP)
    run(go)
