"""Review 1.7, the findings W144 fixed: B-M1 (only the focused field is exempt from a layer), B-M2 (the accessory bar's
window recognised by UIKit's shape only), and the rest listed in each test's docstring. No phone."""
from tapwright import ui

SIZE = (430.0, 932.0)


def _app(inner: str, bundle: str = "com.example.lab") -> str:
    return ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Lab" label="Lab" '
            f'bundleId="{bundle}" x="0" y="0" width="430" height="932">{inner}'
            '</XCUIElementTypeApplication></AppiumAUT>')


def _window(inner: str) -> str:
    return f'<XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="430" height="932">{inner}</XCUIElementTypeWindow>'


def _modal(inside: str) -> str:
    """A full-screen modal: an empty full-screen backdrop, then a card with what it holds."""
    return ('<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" height="932">'
            '<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" height="932"/>'
            '<XCUIElementTypeOther type="XCUIElementTypeOther" x="40" y="300" width="350" height="300">'
            f'{inside}</XCUIElementTypeOther></XCUIElementTypeOther>')


def _form(focused: bool) -> str:
    attr = ' focused="true"' if focused else ""
    return ('<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" height="932">'
            f'<XCUIElementTypeTextField type="XCUIElementTypeTextField" name="amount" label="Amount" x="20" y="100" '
            f'width="390" height="44"{attr}/>'
            '<XCUIElementTypeButton type="XCUIElementTypeButton" name="pay" label="Pay" x="20" y="400" width="390" '
            'height="44"/></XCUIElementTypeOther>')


DELETE = ('<XCUIElementTypeButton type="XCUIElementTypeButton" name="confirm-delete" label="Delete" x="60" y="500" '
          'width="310" height="44"/>')
DELETE_HIGH = DELETE.replace('y="500"', 'y="300"')


def test_b_m1_only_the_focused_field_is_exempt_from_a_layer_the_rest_stays_hidden():
    """B-M1: a field keeps the keyboard focus while the app shows a modal over the form. The field is still listed
    (the focus says it is in front), but the form's Pay button behind the modal is not: it is behind the modal."""
    source = _app(_window('<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" height="932">'
                          f'{_form(True)}{_modal(DELETE)}</XCUIElementTypeOther>'))
    tree = ui.read(source, SIZE)
    labels = [e.label for e in tree.items]
    assert "Amount" in labels and "Delete" in labels
    assert "Pay" not in labels
    assert "Pay" in [e.label for e, _ in tree.behind]


def test_b_m2_an_apps_own_input_view_inside_a_modal_leaves_the_modal_a_layer():
    """B-M2: an element of the app named `inputView` (a natural React Native testID) inside a full-screen modal must not
    make the modal "no layer": the page behind it stays hidden."""
    field = ('<XCUIElementTypeTextField type="XCUIElementTypeTextField" name="inputView" label="Note" x="60" y="320" '
             'width="310" height="44"/>')
    plain = ('<XCUIElementTypeOther type="XCUIElementTypeOther" name="inputView" x="60" y="380" width="310" '
             'height="44"/>')
    for inside in (field + DELETE, plain + DELETE):
        source = _app(_window('<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" '
                              f'height="932">{_form(False)}{_modal(inside)}</XCUIElementTypeOther>'))
        labels = [e.label for e in ui.read(source, SIZE).items]
        assert "Delete" in labels and "Pay" not in labels and "Amount" not in labels


def test_b_m2_a_window_named_like_the_accessory_but_holding_more_is_a_layer():
    """B-M2: a whole second window with an `inputView` bar container AND other actionable content is not the
    accessory bar's window: it hides the window behind it."""
    bar = ('<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="558" width="430" height="374">'
           '<XCUIElementTypeToolbar type="XCUIElementTypeToolbar" name="Toolbar" x="0" y="558" width="430" height="48">'
           '<XCUIElementTypeButton type="XCUIElementTypeButton" name="Flag" label="Flag" x="352" y="563" width="53" '
           'height="38"/></XCUIElementTypeToolbar>'
           '<XCUIElementTypeOther type="XCUIElementTypeOther" name="inputView" x="0" y="614" width="430" height="318"/>'
           '</XCUIElementTypeOther>')
    second = _window('<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" height="932">'
                     '<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" height="932"/>'
                     f'<XCUIElementTypeOther type="XCUIElementTypeOther" x="40" y="200" width="350" height="200">{DELETE_HIGH}'
                     f'</XCUIElementTypeOther>{bar}</XCUIElementTypeOther>')
    source = _app(_window(_form(False)) + second)
    labels = [e.label for e in ui.read(source, SIZE).items]
    assert "Delete" in labels and "Pay" not in labels


# ---- B-M5: the hit test's "ancestor" and another process ----

def test_b_m5_a_window_or_a_full_screen_holder_proves_nothing():
    """B-M5: a cover with no accessibility node of its own makes XCTest's hit test fall back to the Window or a
    full-screen container: that is "holder", not "ancestor", so it neither clears the tree's guess nor passes as the
    target. A real container that holds the target (a list, not the whole screen) is still "ancestor"."""
    from test_runner_v5 import LIST, TREE, WINDOW, find, hit, listed, source
    items, xml = listed(), source()
    email = find(items, "email")
    assert ui.hit_relation(xml, email, hit(LIST)) == "ancestor"
    assert ui.hit_relation(xml, email, hit(WINDOW)) == "holder"
    assert ui.hit_relation(xml, email, hit(WINDOW["c"][0])) == "holder"  # the Window's full-screen view
    assert ui.hit_relation(xml, email, hit(TREE)) == "holder"


def test_b_m5_a_guessed_cover_stands_when_the_hit_is_only_the_window():
    """B-M5: the tree guesses a later overlay covers Cancel; the hit test falls back to the Window. Before 1.7 the
    guess stood; on 8e30e99 the Window counted as "ancestor" and the guess was dropped, so the tap went through."""
    import pytest
    from test_daemon import finish, looked, started
    from test_runner_v5 import SHEET, armed_daemon, hit, sheet_items, source

    from tapwright.control import Refused

    async def go():
        d = armed_daemon(hit(SHEET["c"][0]))
        d.session.last_source = source(SHEET)
        stop, task = await started(d)
        await looked(d, sheet_items())
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"text": "Cancel"}})
        assert e.value.code == "not_actionable" and "tap" not in d.session.calls
        await finish(stop, task)
    from test_daemon import run
    run(go)


def test_b_m5_a_hit_in_another_process_is_covered_unless_its_the_target_or_the_keyboard():
    """B-M5: a banner or share sheet in SpringBoard's process over the target: the touch is refused `covered`, not
    let go on the front-app check. The same element in a remote view the app lists (same/inside) and the keyboard's
    own process still pass."""
    import pytest
    from test_daemon import finish, looked, run, started
    from test_runner_v5 import BAR_BUTTON, ROW, armed_daemon, hit, lab_items

    from tapwright.control import Refused

    async def go():
        d = armed_daemon(hit(BAR_BUTTON, bundle="com.apple.springboard"))
        stop, task = await started(d)
        await looked(d, lab_items())
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "terms-row"}})
        assert e.value.code == "not_actionable" and str(e.value).startswith("covered: ")
        assert "tap" not in d.session.calls
        await finish(stop, task)
        for answer in (hit(ROW, bundle="com.apple.SafariViewService"),
                       hit(BAR_BUTTON, bundle=next(iter(ui.KEYBOARD_HOSTS)))):
            d = armed_daemon(answer)
            stop, task = await started(d)
            await looked(d, lab_items())
            r = await d.run_tool("a", "tap", {"target": {"id": "terms-row"}})
            assert r["text"].startswith("Tapped")
            await finish(stop, task)
    run(go)


# ---- Notification Center: B-M3, B-M4, B-L2, B-M8, B-L1 ----

HOME = ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name=" " label=" " '
        'bundleId="com.apple.springboard" x="0" y="0" width="400" height="800">'
        '<XCUIElementTypeIcon type="XCUIElementTypeIcon" name="Wayfare" label="Wayfare" x="20" y="100" width="64" '
        'height="64"/><XCUIElementTypeOther type="XCUIElementTypeOther" label="Page 1 of 2" x="150" y="700" width="100" '
        'height="20"/></XCUIElementTypeApplication></AppiumAUT>')


def _nc():
    import test_notifications as tn
    return tn


def test_b_m3_springboard_alone_is_no_proof_that_notification_center_is_open(tmp_path):
    """B-M3: a swipe that was swallowed leaves the home screen (SpringBoard's tree, no notifications). That is not
    "no notifications": the tool says Notification Center didn't open, and the trigger's check reads nothing (so the
    seen record never drops its keys). A banner over an app (a notification in no list) proves nothing either."""
    import pytest
    from test_daemon import finish, run, started

    from tapwright import notifications as nl
    from tapwright.control import Refused
    tn = _nc()
    assert not nl.is_center(HOME)
    banner = HOME.replace("</XCUIElementTypeApplication>", tn.cell("Wayfare", "now", "hi") + "</XCUIElementTypeApplication>")
    assert not nl.is_center(banner)
    assert nl.is_center(tn.SCREEN)  # (its title)
    untitled = tn.SCREEN.replace('label="Notification Center"', 'label="Mitteilungen"')
    assert nl.is_center(untitled)  # (notifications in a list)

    async def go():
        d, s = tn.center_daemon(tmp_path, [], source=HOME)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:  # (first: a call makes the phone "in use" for the check for 90 s)
            await d.notification_keys(tn.BUNDLE)
        assert e.value.code == "not_found"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "notifications", {})
        assert e.value.code == "not_found" and tn.center_tool.NOT_OPENED in str(e.value)
        await finish(stop, task)
    run(go)


def _swipe_then_fail(s):
    """The first swipe goes out (Notification Center comes down), then the call raises: a timeout after the gesture."""
    from tapwright.device import DeviceError
    original = s.swipe
    state = {"failed": False}

    async def swipe(*a, **k):
        out = await original(*a, **k)
        if not state["failed"]:
            state["failed"] = True
            raise DeviceError("the swipe's answer timed out")
        return out
    s.swipe = swipe


def test_b_m4_a_swipe_that_raises_after_it_went_out_still_closes_notification_center(tmp_path):
    import pytest
    from test_daemon import finish, run, started

    from tapwright.control import Refused
    tn = _nc()

    async def go():
        d, s = tn.center_daemon(tmp_path, [])
        _swipe_then_fail(s)
        stop, task = await started(d)
        with pytest.raises(Refused):
            await d.run_tool("a", "notifications", {})
        assert not s.center_open and s.foreground == tn.BUNDLE
        await finish(stop, task)
    run(go)


def test_b_m4_a_banner_over_an_app_never_sends_it_home(tmp_path):
    """B-M4: a banner over the app makes WebDriverAgent say SpringBoard. The process list says the app is in front:
    it is brought back after, and the home button is never pressed."""
    from test_daemon import finish, run, started
    tn = _nc()

    async def go():
        d, s = tn.center_daemon(tmp_path, [], foreground="com.apple.springboard")

        async def running():
            return [tn.BUNDLE]
        s.device.foreground_apps = running
        stop, task = await started(d)
        await d.run_tool("a", "notifications", {})
        assert "button:home" not in s.calls and tn.BUNDLE in s.launched and not s.center_open
        await finish(stop, task)
    run(go)


def test_b_l2_notifications_on_a_locked_phone_answers_locked_and_touches_nothing(tmp_path):
    import pytest
    from test_daemon import finish, run, started

    from tapwright.control import Refused
    tn = _nc()

    async def go():
        d, s = tn.center_daemon(tmp_path, [])
        s.is_locked = True
        stop, task = await started(d, until=("locked",))
        with pytest.raises(Refused) as e:
            await d.notification_keys(tn.BUNDLE)
        assert e.value.code == "locked"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "notifications", {})
        assert e.value.code == "locked"
        assert "swipe" not in s.calls and s.center_reads == 0
        await finish(stop, task)
    run(go)


def test_b_m8_open_reads_again_and_refuses_when_the_row_moved(tmp_path):
    """B-M8: a new notification arrives between the read and the tap: the rows shift, and a tap on the first read's
    place would open another app's notification. `open` reads again right before the tap and refuses."""
    import pytest
    from test_daemon import finish, run, started

    from tapwright.control import Refused
    from tapwright.daemon import Scope
    tn = _nc()
    shifted = tn.center(
        tn.cell("Chat", "now", "Ana", "new one", y=100),
        tn.cell("Chat", "now", "Ana", tn.CHAT_SECRET, y=190),
        tn.cell("Wayfare", "2m ago", "Trip booked", "Your trip to Lisbon, Portugal is confirmed", y=280),
        tn.cell("Wayfare", "1h ago", "Price drop", y=370))

    async def go():
        d, s = tn.center_daemon(tmp_path, [])
        s.center_opens = tn.BUNDLE
        original, reads = s.read_center, []

        async def read_center(timeout=10.0):
            reads.append(1)
            if len(reads) > 1:
                s.center_source = shifted
            return await original(timeout)
        s.read_center = read_center
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "notifications", {"open": 1}, scope=Scope((tn.BUNDLE,)))
        assert e.value.code == "not_found" and "moved or changed" in str(e.value)
        assert s.tap_points == [] and not s.center_open and s.foreground == tn.BUNDLE
        await finish(stop, task)
    run(go)


def test_b_m8_the_open_action_is_told_by_its_words_never_a_clear_on_the_left():
    from tapwright import notifications as nl
    tn = _nc()

    def row(*actions):
        return f"<r>{''.join(actions)}</r>"

    def action(label, x):
        return tn.OPEN_BUTTON.replace('label="Open"', f'label="{label}"').replace('x="14"', f'x="{x}"').format(y=100)
    assert nl.open_action(row(action("Clear", 14), action("Open", 320)), 130, 430) is None
    assert nl.open_action(row(action("Open", 14), action("Clear", 320)), 130, 430) == (59.0, 137.0)
    assert nl.open_action(row(action("Ouvrir", 14), action("Effacer", 320)), 130, 430) == (59.0, 137.0)
    assert nl.open_action(row(action("Xyz", 14), action("Abc", 320)), 130, 430) is None  # (unknown words, two actions)
    assert nl.open_action(row(action("Xyz", 14)), 130, 430) == (59.0, 137.0)  # (the row's only action, on the left)
    assert nl.open_action(row(action("Clear", 14)), 130, 430) is None


def test_b_l1_a_banners_app_comes_from_its_labels_first_piece_only():
    """B-L1: a third party's banner whose title is "Messages" (a code spoof) is never taken as Messages'; the app is
    the label's first piece, and it must be the shown name of exactly one app."""
    from tapwright import notifications as nl
    tn = _nc()
    spoof = ('<XCUIElementTypeButton type="XCUIElementTypeButton" name="NotificationShortLookView" label="Spoofy, now" '
             'x="8" y="100" width="414" height="80">'
             '<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Messages" x="20" y="100" width="300" '
             'height="20"/><XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Your code is 123456" '
             'x="20" y="120" width="300" height="20"/></XCUIElementTypeButton>')
    got = nl.parse(tn.center(spoof), {**tn.INSTALLED, "com.example.spoofy": "Spoofy"}, lambda b: True)
    assert [(n.bundle, n.title) for n in got] == [("com.example.spoofy", "Messages")]
    assert nl.parse(tn.center(spoof), tn.INSTALLED, lambda b: True) == []  # (no app of that name: dropped)
