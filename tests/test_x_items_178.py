"""1.7.8: the fixes from the X agent's run (2026-11-17). Each test failed on c367cc5."""
import asyncio
import threading

import pytest
from fakes import button, el
from test_daemon import KEYBOARD_NOTE, finish, keyboard_up, make, run, started
from test_daemon_map import BUNDLE, file_of, mapped

from tapwright import ui
from tapwright.control import Refused

STAYED = "Note: couldn't close the keyboard first: it is still up."


def _clear(point, rects, margin):
    x, y = point
    return all(not (r[0] - margin <= x <= r[0] + r[2] + margin and r[1] - margin <= y <= r[1] + r[3] + margin)
               for r in rects)


# ---- item 1: the keyboard reflex's drag never starts or ends on a control ----

def test_1_the_reflex_drag_keeps_off_a_control_at_the_top_of_the_form():
    """X's composer: a short drag where the reflex used to put it (the middle of the form, near its top) lay on a
    control. The drag now runs on passive content only, every point of it clear of what takes a touch."""
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False)
        photo = button("e9", "Photo library", "photo", rect=(180, 70, 40, 40))
        d.session.tree = [*d.session.tree, el("e8", "scroll", "", "form", rect=(0, 60, 400, 440)), photo]
        await d.run_tool("a", "look", {"screenshot": False})
        drags = []
        real = d.session.swipe

        async def swipe(x0, y0, x1, y1, **kw):
            drags.append((x0, y0, x1, y1))
            out = await real(x0, y0, x1, y1, **kw)
            d.session.last_keyboard = False
            return out
        d.session.swipe = swipe
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}})
        assert len(drags) == 1
        controls = [e.rect for e in d.session.tree if e.actions and e.role != "scroll"]
        x0, y0, x1, y1 = drags[0]
        assert _clear((x0, y0), controls, 11.9) and _clear((x1, y1), controls, 11.9)
        assert "Reflex: closed the keyboard (dragged the form a little)." in r["text"].splitlines()
        await finish(stop, task)
    run(go)


def test_1_with_no_passive_line_there_is_no_drag_and_the_answer_says_the_keyboard_stayed():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False)
        rows = [button(f"r{i}", f"Row {i}", f"row{i}", rect=(0, 60 + 40 * i, 400, 40)) for i in range(11)]
        d.session.tree = [el("e8", "scroll", "", "form", rect=(0, 60, 400, 440)), *rows,
                          el("e2", "input", "Field A", "a", rect=(10, 100, 300, 40))]
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "tap", {"target": {"id": "row6"}})
        assert "swipe" not in d.session.calls
        assert STAYED[len("Note: "):] in r["text"] and KEYBOARD_NOTE not in r["text"], r["text"]
        await finish(stop, task)
    run(go)


def test_1_a_reflex_that_opens_another_screen_stops_the_touch():
    """The reflex's touch brought X's photo picker; the agent's tap at a point then landed on the picker. Now the
    call stops there: stale, and the point is never touched."""
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False)
        d.session.tree = [*d.session.tree, el("e8", "scroll", "", "form", rect=(0, 60, 400, 440))]
        await d.run_tool("a", "look", {"screenshot": False})
        real = d.session.swipe
        picker = [button(f"p{i}", f"Photo {i}", f"photo{i}", rect=(0, 100 + 50 * i, 400, 50)) for i in range(6)]

        async def swipe(*a, **kw):
            out = await real(*a, **kw)
            d.session.tree, d.session.last_keyboard = picker, False  # the photo picker took the keyboard's place
            return out
        d.session.swipe = swipe
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"x": 375, "y": 81}})
        assert e.value.code == "stale" and "opened another screen" in str(e.value)
        assert (375.0, 81.0) not in d.session.tap_points
        await finish(stop, task)
    run(go)


# ---- item 3: a label with a narrow no-break space matches as it is listed ----

@pytest.mark.parametrize("space", [" ", " ", "  "])
def test_3_a_label_with_another_space_matches_the_listed_text(space):
    cell = el("e12", "image", f"Video, twelve seconds, 17 November, 7:19{space}PM", "PXGGridLayout-Info",
              rect=(-1, 116, 144, 143))
    other = el("e13", "image", "Video, fifty-two seconds, 17 November, 6:21 PM", "PXGGridLayout-Info",
               rect=(143, 116, 144, 143))
    shown = cell.line()
    assert "7:19 PM" in shown  # as the agent sees it
    got = ui.resolve([cell, other], ui.Target(label="Video, twelve seconds, 17 November, 7:19 PM"))
    assert got is cell
    with pytest.raises(Refused):  # still exact: a part of it is no label
        ui.resolve([cell, other], ui.Target(label="Video, twelve seconds"))


# ---- item 4: typing without a target goes only to the app the last touch went to ----

def test_4_typing_without_a_target_after_another_app_came_to_the_front_is_stale():
    async def go():
        d = make()
        s = d.session
        s.app = ("Files", "com.apple.DocumentsApp")
        s.tree = [el("e1", "input", "Search", "search", rect=(20, 67, 259, 48)), button("e2", "Close", "close", rect=(362, 67, 48, 48))]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"label": "Search"}})
        s.app = ("Safari", "com.apple.mobilesafari")  # Safari came back to the front after the tap
        s.tree = [el("e1", "input", "Address", "TabBarItemTitle", rect=(145, 861, 140, 26))]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"text": "tapwright-square"})
        assert e.value.code == "stale" and "Safari" in str(e.value) and "Files" in str(e.value)
        assert s.typed == []
        await d.run_tool("a", "look", {"screenshot": False})  # the agent sees Safari now: it may type there
        await d.run_tool("a", "type_text", {"text": "x"})
        assert len(s.typed) == 1
        await finish(stop, task)
    run(go)


# ---- item 5: no value read before the submit is reported as the field's value ----

def test_5_after_a_submit_the_answer_never_gives_the_value_from_before():
    async def go():
        d = make()
        s = d.session
        s.tree = [el("e1", "input", "Address", "URL", rect=(20, 100, 390, 40), value="old.example/index.html")]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        real = s.type_text

        async def type_text(text, **kw):
            out = await real(text, **kw)
            # the page loaded: Safari's address field is another element, showing the new host
            s.tree = [el("e1", "input", "Address", "TabBarItemTitle", rect=(145, 861, 140, 26), value="new.example")]
            return out
        s.type_text = type_text
        r = await d.run_tool("a", "type_text", {"target": {"id": "URL"}, "text": "new.example", "submit": True})
        first = r["text"].splitlines()[0]
        assert "old.example" not in first and first.startswith('Typed into e1 input "Address"')
        await finish(stop, task)
    run(go)


# ---- item 6: forgetting always wins over a save under way ----

def test_6_a_forgotten_reflex_written_back_by_a_save_under_way_stays_forgotten_after_another_edit(tmp_path):
    """CI (ubuntu, 3.10): a save under way wrote the forgotten reflex back into the file; the next edit (`forget
    home`) read the file, saw the reflex in it and took it for one the agent wanted, so it came back for good."""
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "map", {"name": "home"})
        rate = {"name": "rate_prompt", "when": {"text": "Rate Wayfare"}, "do": {"tool": "tap", "target": {"text": "Not now"}}}
        await d.run_tool("a", "map", {"reflex": rate})
        await d.maps.flush()
        file = d.maps.file(None, BUNDLE)
        real_save = file.save
        entered, go_on, written, release = (threading.Event() for _ in range(4))

        def held_save(memory):  # the background save: its snapshot (with the reflex) is written after the forget
            entered.set()
            assert go_on.wait(10)
            merged = real_save(memory)
            written.set()
            assert release.wait(10)
            return merged
        file.save = held_save
        d.maps._changed(None, BUNDLE)
        wait = asyncio.get_running_loop().run_in_executor
        assert await wait(None, entered.wait, 10)
        file.save = real_save
        assert (await d.run_tool("a", "map", {"forget_reflex": "rate_prompt"}))["text"] == "Reflex removed: rate_prompt."
        go_on.set()
        assert await wait(None, written.wait, 10)
        assert any(r["name"] == "rate_prompt" for r in file_of(tmp_path)["reflexes"])  # written back meanwhile
        await d.run_tool("a", "map", {"forget": "home"})  # an unrelated edit while the save is still under way
        release.set()
        await d.maps.flush()
        assert file_of(tmp_path)["reflexes"] == []
        assert d.maps.data(None, BUNDLE)["reflexes"] == []
        assert "home" not in file_of(tmp_path)["places"]
        await finish(stop, task)
    run(go)


# ---- item 7: iOS 26's Notification Center is proven by its cells or the cover sheet's date ----

def _springboard(inner: str) -> str:
    return ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name=" " label=" " '
            'bundleId="com.apple.springboard" x="0" y="0" width="430" height="932">'
            '<XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="430" height="932">'
            f'<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" height="932">{inner}'
            '</XCUIElementTypeOther></XCUIElementTypeWindow></XCUIElementTypeApplication></AppiumAUT>')


DATE = ('<XCUIElementTypeOther type="XCUIElementTypeOther" name="lockscreen-date-view" label="Tuesday 17 November" '
        'x="0" y="{y}" width="430" height="{h}"/>')


def test_7_ios26_cover_sheet_without_title_or_scroll_view_is_notification_center():
    from test_notifications import ios26_cell

    from tapwright import notifications as nl
    cells = _springboard(ios26_cell("Wayfare", "now", "Trip booked", "Lisbon", y=300))  # no title, no list
    assert nl.is_center(cells)
    assert nl.is_center(_springboard(DATE.format(y=120, h=80)))  # nothing to show: the date proves the sheet
    # fail-safe: the home screen, a banner on it, a date view off the screen or with no size prove nothing
    banner = ('<XCUIElementTypeOther type="XCUIElementTypeOther" name="NotificationShortLookView" label="Mail, now" '
              'x="8" y="60" width="414" height="80"/>')
    assert not nl.is_center(_springboard(banner))
    assert not nl.is_center(_springboard(DATE.format(y=-900, h=80)))
    assert not nl.is_center(_springboard(DATE.format(y=120, h=0)))
    assert not nl.is_center(cells.replace('bundleId="com.apple.springboard"', 'bundleId="com.apple.Preferences"'))
