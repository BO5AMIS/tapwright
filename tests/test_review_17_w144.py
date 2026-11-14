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
