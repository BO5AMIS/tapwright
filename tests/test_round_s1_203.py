"""Tapwright 2.0.3, round S, S-F1 (the showcase's practice on f5eb807): on a shopping app the path home -> search ->
results tripped the app-wide stop on long reads before results was ever tried (home cut, home with its carousel moved
cut, the search screen cut while the app was busy redrawing), so the whole session got 0 elements. Now the app-wide
stop needs three failures in a row AND no whole long read of the app ever; a long read cut while the app was busy
(XCTest lost its application element mid-read) isn't counted; and type_text's own read for its field asks no long
budget."""
from test_long_block_screen_203 import _on, _shop
from test_slow_readable_203 import CUT, SHOP, WHOLE, ShopRunner, Stream, budget, look, run, state  # noqa: F401
from test_vision_200 import _app

from tapwright import session as session_module

BUSY = {"token": "b", "tree": _app(SHOP, "Shop"), "tier": "root", "truncated": True,
        "why": "full: The previously found element \"Application 'com.example.shop'\" is not present in the current view "
               "anymore. Make sure the application UI has the expected state."}


def test_s_f1_home_carousel_and_a_busy_search_screen_leave_results_its_long_try(state):
    async def go():
        stream = Stream()
        s, runner = _shop(stream, ShopRunner(stream, answers={}))
        _on(stream, b"home")
        await look(s, stream)  # the home: never reads (a carousel)
        assert runner.budgets() == [3000, 8000] and s.budgets.misses(SHOP) == 1
        _on(stream, b"home, its carousel moved")
        stream.now[0] += 20.0
        await look(s, stream)  # another screen than the one the long read failed on: a long try, cut
        assert runner.budgets()[2:] == [3000, 8000] and s.budgets.misses(SHOP) == 2
        runner.answers = {3000: BUSY, 8000: BUSY}
        _on(stream, b"search, keyboard up")
        stream.now[0] += 20.0
        await look(s, stream)  # cut while the app was busy: says nothing of the screen, not counted
        assert runner.budgets()[4:] == [3000, 8000] and s.budgets.misses(SHOP) == 2
        assert s.budgets.block_state(SHOP) != "app"
        runner.answers = {3000: CUT, 8000: WHOLE}
        _on(stream, b"results")
        stream.now[0] += 20.0
        tree = await look(s, stream)
        assert runner.budgets()[6:] == [3000, 8000] and tree.tier == "full"  # (results gets its long try, whole)
    run(go)


def test_an_app_none_of_whose_screens_ever_reads_still_gets_the_app_wide_stop(state):
    async def go():
        stream = Stream()
        s, runner = _shop(stream, ShopRunner(stream, answers={}))
        for n in range(3):
            _on(stream, b"page %d" % n)
            await look(s, stream)
            stream.now[0] += 5.0
        _on(stream, b"page 4")
        await look(s, stream)
        assert runner.budgets().count(8000) == 3 and s.budgets.block_state(SHOP) == "app"
    run(go)


def test_after_one_whole_long_read_three_failures_in_a_row_block_only_their_screens(state):
    async def go():
        stream = Stream()
        s, runner = _shop(stream, ShopRunner(stream))
        _on(stream, b"home")
        await look(s, stream)  # the home reads whole with the long try: some screens of the app read
        assert s.budgets.slow(SHOP)
        runner.answers = {}
        for n in range(3):  # three pages, each planned long read cut
            _on(stream, b"page %d" % n)
            stream.now[0] += 5.0
            await look(s, stream)
        assert s.budgets.misses(SHOP) == 3 and s.budgets.block_state(SHOP) != "app"  # (three in a row: not the app's)
        reads = runner.budgets().count(8000)
        stream.now[0] += 5.0
        await look(s, stream)  # the failed page again, inside its block: no long read
        assert runner.budgets().count(8000) == reads
        runner.answers = {3000: CUT, 8000: WHOLE}
        _on(stream, b"results")
        stream.now[0] += 5.0
        tree = await look(s, stream)  # another screen: its long read goes, and reads whole
        assert runner.budgets().count(8000) == reads + 1 and tree.tier == "full"
    run(go)


def test_a_busy_long_read_keeps_the_count_and_its_screens_block():
    clock = [100.0]
    b = budget(clock=lambda: clock[0])
    b.other = lambda picture: False  # (the screen still looks like the one it failed on)
    b.long_sent(SHOP, "page")
    b.answered(SHOP, True, 8000)
    b.long_sent(SHOP, "search")
    b.answered(SHOP, True, 8000, busy=True)
    assert b.misses(SHOP) == 1 and b.block_state(SHOP) == "same"  # (not counted; its screen blocked as any cut one)


def test_type_texts_own_read_asks_no_long_budget(state):
    async def go():
        stream = Stream()
        s, runner = _shop(stream, ShopRunner(stream, answers={}))
        _on(stream, b"search")
        token = session_module.NO_LONG.set(True)
        try:
            await look(s, stream)
        finally:
            session_module.NO_LONG.reset(token)
        assert 8000 not in runner.budgets() and SHOP not in s.budgets._long_misses
    run(go)


def test_type_text_without_a_target_reads_its_field_with_no_long_budget():
    from fakes import DescribingSession, el
    from test_daemon import finish, make, started

    async def go():
        s = DescribingSession()
        s.tree = [el("e1", "input", "Search", "search", rect=(20, 67, 259, 48))]
        d = make(s)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        seen = []
        real = s.refresh

        async def refresh(*a, **k):
            seen.append(session_module.NO_LONG.get())
            return await real(*a, **k)
        s.refresh = refresh
        await d.run_tool("a", "type_text", {"text": "milk"})
        assert seen and seen[0] is True and session_module.NO_LONG.get() is False
        await finish(stop, task)
    run(go)
