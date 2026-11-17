"""1.7.6, from the demo-video report (2026-11-16, Tapwright 1.7.5 on a USB iPhone):
1. a target given as a string is refused bad_params, and the action log never turns that into an internal error;
2. a Back button an app lays on its navigation bar as a sibling (X's profile page) is the bar's Back, and is not
   covered by its own bar, while content scrolled under the bar still is."""
import json

from test_daemon import finish, make, run, started

from tapwright import actionlog, ui
from tapwright.control import Refused


def test_a_target_that_isnt_an_object_is_logged_by_its_kind_and_serialises():
    assert actionlog.target_of("Workouts") == {"given": "str"}
    assert actionlog.target_of(["e3"]) == {"given": "list"}
    json.dumps(actionlog.target_of("Workouts"))
    assert actionlog.target_of({"text": "Workouts", "index": 1}) == {"text": "Workouts", "index": 1}
    assert actionlog.target_of(ui.Target(ref="e3")) == {"ref": "e3"}


def test_a_string_target_is_refused_bad_params_not_internal(tmp_path):
    from tapwright.actionlog import ActionLog

    async def go():
        d = make(actions=ActionLog(tmp_path / "log"))
        stop, task = await started(d)
        try:
            await d.run_tool("demo", "tap", {"target": "Workouts"})
            raise AssertionError("not refused")
        except Refused as e:
            assert e.code == "bad_params" and "target must be an object" in str(e)
        lines = [json.loads(x) for p in (tmp_path / "log").glob("*.jsonl") for x in p.read_text("utf-8").splitlines()]
        assert lines and lines[-1]["code"] == "bad_params" and lines[-1]["target"] == {"given": "str"}
        await finish(stop, task)
    run(go)


def test_a_failing_log_line_never_replaces_the_refusal(monkeypatch):
    async def go():
        d = make()
        stop, task = await started(d)

        def broken(*a, **k):
            raise TypeError("not serialisable")
        monkeypatch.setattr(d, "_log_action", broken)
        try:
            await d.run_tool("demo", "tap", {"target": "Workouts"})
            raise AssertionError("not refused")
        except Refused as e:
            assert e.code == "bad_params"
        await finish(stop, task)
    run(go)


def _profile(content_button: bool = False) -> str:
    """X's profile page as the report shows it: a navigation bar, and a Back button that is the bar's sibling, laid over
    it. With `content_button`, a list row's button scrolled under the bar too."""
    row = ('<XCUIElementTypeTable type="XCUIElementTypeTable" x="0" y="0" width="430" height="932">'
           '<XCUIElementTypeCell type="XCUIElementTypeCell" x="0" y="60" width="430" height="50">'
           '<XCUIElementTypeButton type="XCUIElementTypeButton" name="follow" label="Follow" x="300" y="70" '
           'width="80" height="30"/></XCUIElementTypeCell></XCUIElementTypeTable>') if content_button else ""
    return ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name="X" label="X" '
            'bundleId="com.example.x" x="0" y="0" width="430" height="932">'
            '<XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="430" height="932">'
            f'<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" height="932">{row}'
            '<XCUIElementTypeNavigationBar type="XCUIElementTypeNavigationBar" name="TFNNavigationBarAXIdentifier" '
            'x="0" y="59" width="430" height="56"/>'
            '<XCUIElementTypeButton type="XCUIElementTypeButton" name="NavigationBackButton" label="Back" x="16" '
            'y="67" width="40" height="40"/>'
            '</XCUIElementTypeOther></XCUIElementTypeWindow></XCUIElementTypeApplication></AppiumAUT>')


def test_a_back_button_laid_on_the_bar_is_the_bars_back_and_not_covered_by_it():
    tree = ui.read(_profile(), (430.0, 932.0))
    back = next(e for e in tree.items if e.identifier == "NavigationBackButton")
    assert not any(b.kind == "NavigationBar" for b in back.covered_by)
    assert tree.back is not None and tree.back.point == (36.0, 87.0)


def test_content_scrolled_under_the_bar_is_still_covered():
    tree = ui.read(_profile(content_button=True), (430.0, 932.0))
    follow = next(e for e in tree.items if e.identifier == "follow")
    assert any(b.kind == "NavigationBar" for b in follow.covered_by)
