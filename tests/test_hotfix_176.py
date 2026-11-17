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

        called: list = []

        def broken(*a, **k):
            called.append(a[0])
            raise TypeError("not serialisable")
        monkeypatch.setattr(d, "_log_action", broken)
        try:
            await d.run_tool("demo", "tap", {"target": "Workouts"})
            raise AssertionError("not refused")
        except Refused as e:
            assert e.code == "bad_params"
        assert called == ["tap"]  # (the log was reached, failed, and the refusal still came through)
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


def _bar_screen(children: str, bar: str = "NavigationBar") -> str:
    return ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name="X" label="X" '
            'bundleId="com.example.x" x="0" y="0" width="430" height="932">'
            '<XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="430" height="932">'
            f'<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="430" height="932">'
            + children.replace("BAR", f'<XCUIElementType{bar} type="XCUIElementType{bar}" name="bar" '
                                      f'x="0" y="{59 if bar == "NavigationBar" else 820}" width="430" height="56"/>')
            + '</XCUIElementTypeOther></XCUIElementTypeWindow></XCUIElementTypeApplication></AppiumAUT>')


def _button(name: str, label: str, y: int = 67, x: int = 16) -> str:
    return (f'<XCUIElementTypeButton type="XCUIElementTypeButton" name="{name}" label="{label}" x="{x}" y="{y}" '
            f'width="40" height="40"/>')


def test_a_button_laid_on_the_bar_that_isnt_back_is_never_taken_for_back():
    """Opus review M1: X's home screen lays its avatar on the bar; with no Back there, back must say so."""
    tree = ui.read(_bar_screen("BAR" + _button("ProfileAvatar", "Your profile")), (430.0, 932.0))
    assert tree.back is not None and tree.back.bar and tree.back.point is None
    avatar = next(e for e in tree.items if e.identifier == "ProfileAvatar")
    assert any(b.kind == "NavigationBar" for b in avatar.covered_by)  # (only a Back control's cover is lifted)


def test_content_listed_before_a_see_through_bar_is_still_covered():
    """Opus review M2: a map pin under a translucent bar isn't in a scroll view, but it comes before the bar."""
    tree = ui.read(_bar_screen(_button("pin", "Pin") + "BAR"), (430.0, 932.0))
    pin = next(e for e in tree.items if e.identifier == "pin")
    assert any(b.kind == "NavigationBar" for b in pin.covered_by)
    assert tree.back.point is None  # (a button under the bar is never its Back)


def test_a_static_control_after_a_bar_keeps_its_cover():
    """Opus review 2 of 1.7.6: real trees list the bar first and the content under it after, so order proves nothing; only
    a Back control's cover is lifted. A Share button after a toolbar, a header's button after a navigation bar: covered."""
    tool = ui.read(_bar_screen("BAR" + _button("share", "Share", y=828), "Toolbar"), (430.0, 932.0))
    head = ui.read(_bar_screen("BAR" + _button("compose", "Compose", x=360)), (430.0, 932.0))
    assert any(b.kind == "Toolbar" for b in next(e for e in tool.items if e.identifier == "share").covered_by)
    assert any(b.kind == "NavigationBar" for b in next(e for e in head.items if e.identifier == "compose").covered_by)
    back_to_top = ui.read(_bar_screen("BAR" + _button("Back to top", "Back to top")), (430.0, 932.0))
    assert back_to_top.back.point is None


def test_a_log_entry_that_isnt_json_is_skipped_never_raised(tmp_path):
    from tapwright.actionlog import ActionLog
    said: list = []
    log = ActionLog(tmp_path / "log", report=said.append)
    log.write({"tool": "tap", "target": {"index": str.index}})  # (as 1.7.5 built it for a string target)
    assert said and "skipped" in said[0]
    log.write({"tool": "tap", "ok": True})
    assert any(p.read_text("utf-8") for p in (tmp_path / "log").glob("*.jsonl"))


def test_back_is_a_word_of_the_id_never_inside_another_word():
    """CodeRabbit, PR #16: a FeedbackButton laid on the bar is not Back; NavigationBackButton is."""
    feedback = ui.read(_bar_screen("BAR" + _button("FeedbackButton", "Feedback")), (430.0, 932.0))
    assert feedback.back.point is None
    back = ui.read(_bar_screen("BAR" + _button("NavigationBackButton", "")), (430.0, 932.0))
    assert back.back.point == (36.0, 87.0)
    assert [ui._says_back(n) for n in ("nav-back-button", "BackButton", "backgroundView")] == [True, True, False]


def test_the_runtime_notes_name_no_old_release():
    """CodeRabbit, PR #16: the whole-phone note printed "Tapwright 1.7.5" in later releases."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "tapwright"
    for name in ("cli.py", "mcp_server.py", "thin.py"):
        assert "Tapwright 1.7.5 provides" not in (root / name).read_text("utf-8"), name
