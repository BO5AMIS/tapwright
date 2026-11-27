"""Tapwright 2.0.3, round N part 5 (the showcase's practice on the phone, N-F7): an o ref tapped 6 s after the look, on a
shopping app's home whose list was cut short, went to another control (the home had moved; its search field's
placeholder also changes every few seconds) and nothing said so: the picture rule (_moved_under) is off on such screens
by design. Now a touch aimed by an o ref reads the picture's text once more right before it goes, and goes only when
the same text is still within O_REF_STILL of where the look read it. Also: a look's picture was refused "the phone is
being unlocked" half an hour after the only unlock (its frame had dropped out of the hub's history)."""
import asyncio

from fakes import DescribingSession
from test_daemon import finish, make, started
from test_touch_other_app_203 import SHOP, quick_banner, refused, run  # noqa: F401 (fixture)

from tapwright import daemon as daemon_module
from tapwright import ocr

SEARCH = ocr.Seen("Search for shoes", (30.0, 330.0, 200.0, 20.0), 1.0)  # (centre 130, 340)
BANNER = ocr.Seen("New arrivals", (30.0, 500.0, 120.0, 20.0), 1.0)


def at(seen: ocr.Seen, dy: float) -> ocr.Seen:
    x, y, w, h = seen.rect
    return ocr.Seen(seen.text, (x, y + dy, w, h), seen.score)


def shop():
    s = DescribingSession()
    s.tier, s.tree, s.app = "root", [], SHOP
    s.runner_text = [SEARCH, BANNER]
    return s


async def looked(s):
    d = make(s)
    stop, task = await started(d)
    r = await d.run_tool("a", "look", {"screenshot": False})
    assert 'o1 "Search for shoes" @30,330 200x20' in r["text"].splitlines()
    return d, stop, task


def test_n_f7_the_text_gone_is_stale_and_nothing_is_touched():
    async def go():
        s = shop()
        d, stop, task = await looked(s)
        s.runner_text = [BANNER]  # (the placeholder changed, the home moved)
        e = await refused(d, "tap", {"target": {"ref": "o1"}, "look": False})
        assert e.code == "stale" and str(e) == ('o1 "Search for shoes" is no longer where your last look read it; '
                                               "nothing was touched. Look again.")
        assert "tap" not in s.calls and s.text_levels == ["accurate", "accurate"]
        await finish(stop, task)
    run(go)


def test_n_f7_the_text_moved_190_pt_is_stale_and_says_where_never_touching_there():
    async def go():
        s = shop()
        d, stop, task = await looked(s)
        s.runner_text = [at(SEARCH, -190.0), BANNER]
        e = await refused(d, "tap", {"target": {"ref": "o1"}, "look": False})
        assert e.code == "stale" and "(it is at (130, 150) now)" in str(e) and "tap" not in s.calls
        await finish(stop, task)
    run(go)


def test_n_f7_the_text_8_pt_off_is_touched_at_its_centre_now():
    async def go():
        s = shop()
        d, stop, task = await looked(s)
        s.runner_text = [at(SEARCH, 8.0), BANNER]
        r = await d.run_tool("a", "tap", {"target": {"ref": "o1"}, "look": False})
        assert r["text"].startswith("Tapped (130, 348).") and s.calls.count("tap") == 1
        await finish(stop, task)
    run(go)


def test_n_f7_the_picture_unreadable_now_is_stale():
    async def go():
        s = shop()
        d, stop, task = await looked(s)
        s.runner_text = RuntimeError("the phone's OCR failed")  # (and this computer has no OCR: the fixture)
        e = await refused(d, "tap", {"target": {"ref": "o1"}, "look": False})
        assert e.code == "stale" and "(the picture can't be read now)" in str(e) and "tap" not in s.calls
        await finish(stop, task)
    run(go)


def test_n_f7_force_touches_where_the_look_read_it_without_reading_again():
    async def go():
        s = shop()
        d, stop, task = await looked(s)
        s.runner_text = []
        r = await d.run_tool("a", "tap", {"target": {"ref": "o1"}, "look": False, "force": True})
        assert r["text"].startswith("Tapped (130, 340).") and s.text_levels == ["accurate"]
        await finish(stop, task)
    run(go)


def test_n_f7_a_dry_run_runs_the_check():
    async def go():
        s = shop()
        d, stop, task = await looked(s)
        s.runner_text = [at(SEARCH, -190.0)]
        e = await refused(d, "tap", {"target": {"ref": "o1"}, "dry_run": True})
        assert e.code == "stale" and "tap" not in s.calls
        s.runner_text = [SEARCH]
        r = await d.run_tool("a", "tap", {"target": {"ref": "o1"}, "dry_run": True})
        assert r["text"].startswith("Would tap at 130,340") and "tap" not in s.calls
        await finish(stop, task)
    run(go)


def test_a_playing_feed_whose_caption_stays_is_touched():
    async def go():
        s = shop()
        d, stop, task = await looked(s)
        s.runner_text = [SEARCH, ocr.Seen("Other caption", (30.0, 500.0, 120.0, 20.0), 1.0)]  # (the rest plays on)
        r = await d.run_tool("a", "tap", {"target": {"ref": "o1"}, "look": False})
        assert r["text"].startswith("Tapped (130, 340).")
        await finish(stop, task)
    run(go)


def test_the_checks_read_is_no_look_the_o_refs_stay():
    async def go():
        s = shop()
        d, stop, task = await looked(s)
        view = d.control.client("a")
        refs, gen = dict(view.ocr), view.ocr_gen
        s.runner_text = [BANNER]
        await refused(d, "tap", {"target": {"ref": "o1"}, "look": False})
        assert view.ocr == refs and view.ocr_gen == gen
        s.runner_text = [SEARCH, BANNER]  # (back where it was: the same o ref touches it)
        r = await d.run_tool("a", "tap", {"target": {"ref": "o1"}, "look": False})
        assert r["text"].startswith("Tapped (130, 340).")
        await finish(stop, task)
    run(go)


def test_the_case_and_spaces_are_matched_as_the_pictures_text_is():
    async def go():
        s = shop()
        d, stop, task = await looked(s)
        s.runner_text = [ocr.Seen("search  for SHOES", SEARCH.rect, 1.0)]
        r = await d.run_tool("a", "tap", {"target": {"ref": "o1"}, "look": False})
        assert r["text"].startswith("Tapped (130, 340).")
        await finish(stop, task)
    run(go)


def test_o_ref_still_is_twelve_points():
    assert daemon_module.O_REF_STILL == 12.0


# ---- a frame that dropped out of the hub's history is no picture of an unlock long over ----

def test_a_frame_out_of_the_hubs_history_is_judged_by_its_own_stamp():
    async def go():
        d = make(DescribingSession())
        now = [1000.0]
        d.hub._mono = lambda: now[0]
        span = d._unlock_begin()
        d._unlock_end(span)  # (the night's only unlock, ended)
        now[0] += 1800.0  # (half an hour later)
        old = d.hub.publish(b"\xff\xd8read")
        for _ in range(80):  # (a long read: the read's frame drops out of the kept history)
            d.hub.publish(b"\xff\xd8more")
        assert d.hub.arrival_of(old.seq) is None
        assert not d.frame_private(old)
        # a frame from inside an unlock's span is still private when it dropped out (judged by its stamp)
        span = d._unlock_begin()
        during = d.hub.publish(b"\xff\xd8keys")
        for _ in range(80):
            d.hub.publish(b"\xff\xd8more")
        d._unlock_end(span)
        assert d.hub.arrival_of(during.seq) is None and d.frame_private(during)

        class Unstamped:
            seq, picture = -1, None
        assert d.frame_private(Unstamped())  # (no stamp: fail-closed while an unlock span is kept)
    asyncio.run(go())
