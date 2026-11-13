"""Reflexes through the daemon (AGENT-TOOLS §18.7): declared ones between the steps of a call, and the built-in two."""
import dataclasses

import pytest
from fakes import FakeSession, button, el
from mapfakes import APP, MapSession, screen
from test_daemon import KEYBOARD_BAR, finish, keyboard_up, make, run, started

from tapwright import mapkeeper, ui
from tapwright.control import Refused

BUNDLE = APP[1]
RATE = {"name": "rate_prompt", "when": {"text": "Rate Wayfare"}, "do": {"tool": "tap", "target": {"id": "not-now"}}}
LINE = 'Reflex: closed "Rate Wayfare" (tapped id=not-now).'
GOES = {("home", "settings-btn"): "settings", ("rate", "not-now"): "home", ("rate", "settings-btn"): "settings",
        ("rate2", "not-now"): "rate", ("home2", "not-now"): "home"}


def edit(tree: ui.Tree, drop: tuple[str, ...] = (), **changes) -> ui.Tree:
    """The same screen (its structure, so its place) with some elements gone or changed."""
    items = []
    for e in tree.items:
        if e.identifier in drop:
            continue
        items.append(dataclasses.replace(e, **changes[e.identifier]) if e.identifier in changes else e)
    return dataclasses.replace(tree, items=items)


def screens():
    home = screen("Home", ids=("settings-btn", "h1", "h2"), tab="home")
    prompt = screen("Home", ids=("settings-btn", "h1", "h2", "not-now"), tab="home", texts=("Rate Wayfare",))
    prompt = edit(prompt, **{"not-now": {"rect": (10.0, 300.0, 200.0, 40.0)}})  # (above the tab bar: a person can reach it)
    return {"home": home,
            "rate": edit(prompt, drop=("settings-btn",)),  # the prompt is over the page: its button isn't there to find
            "rate2": edit(prompt, drop=("settings-btn",)),
            "covered": edit(prompt, **{"settings-btn": {"enabled": False}}),
            "settings": screen("Settings", ids=("language-row", "about-row", "s1", "s2")),
            "labelled": edit(prompt, drop=("settings-btn",), **{"not-now": {"label": "Delete account"}}),
            "alerting": screen("Home", ids=("h1", "h2", "not-now"), tab="home", texts=("Rate Wayfare",), alert="Allow notifications?")}


def mapped(tmp_path, start="home", goes=None):
    d = make(MapSession(screens(), start, {**GOES, **(goes or {})}))
    d.maps = mapkeeper.Maps(str(tmp_path))
    return d


async def show(d, name):
    """The phone shows another screen, and the agent looks at it (it isn't told otherwise)."""
    d.session.at = name
    await d.run_tool("a", "look", {"screenshot": False})


async def ready(d, reflex=RATE):
    """The app has a map with its home place, and the reflex is declared."""
    d.session.at = "home"
    await d.run_tool("a", "look", {"screenshot": False})
    await d.run_tool("a", "map", {"name": "home"})
    await d.run_tool("a", "map", {"reflex": reflex})
    await show(d, "rate")


def test_a_step_that_cant_find_its_target_fires_a_declared_reflex_and_goes_on(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await ready(d)
        r = await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "settings-btn"}}]})
        assert r["text"].startswith("Ran 1 of 1 steps") and LINE in r["text"].splitlines() and d.session.at == "settings"
        assert d.session.tap_points and len(d.session.tap_points) == 2  # not-now, then the step's own touch
        await finish(stop, task)
    run(go)


def test_reflexes_are_checked_before_answering_a_failure_not_between_every_step_of_a_run(tmp_path):
    """AGENT-TOOLS 1.13.1 (10): a prompt on screen that doesn't stop a step is left alone, step after step; the check
    comes when a step fails the way a prompt over its target makes it fail."""
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await ready(d)
        both = screens()["rate"]
        d.session.screens["both"] = edit(both, **{"not-now": {"rect": (10.0, 300.0, 200.0, 40.0)}})
        await show(d, "both")  # the prompt is up, and the page under it can still be touched
        before = len(d.session.tap_points)
        r = await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "h1"}},
                                                    {"tool": "tap", "target": {"id": "h2"}}]})
        assert r["text"].startswith("Ran 2 of 2 steps") and "Reflex:" not in r["text"]
        assert len(d.session.tap_points) - before == 2  # the two steps' own touches: no not-now in between
        await finish(stop, task)
    run(go)


def test_it_fires_at_most_max_times_in_one_call(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await ready(d)
        await show(d, "rate2")  # a second prompt behind the first
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "settings-btn"}}]})
        assert e.value.code == "not_found" and len(d.session.tap_points) == 1  # once
        await show(d, "rate2")
        await d.run_tool("a", "map", {"reflex": {**RATE, "max": 2}})
        r = await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "settings-btn"}}]})
        assert r["text"].count("Reflex: closed") == 2 and d.session.at == "settings"
        await finish(stop, task)
    run(go)


def test_a_reflex_with_a_place_fires_only_there(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await ready(d)
        await show(d, "settings")
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "map", {"name": "settings"})
        await d.run_tool("a", "map", {"reflex": {**RATE, "at": "settings"}})
        await show(d, "rate")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "settings-btn"}}]})
        assert e.value.code == "not_found" and d.session.tap_points == []
        await finish(stop, task)
    run(go)


def test_a_reflex_whose_target_carries_a_policy_word_now_does_not_fire_and_asks_no_one(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await ready(d)
        await show(d, "labelled")  # the same id, but the button now says Delete account
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "settings-btn"}}]})
        assert e.value.code == "not_found" and d.session.tap_points == [] and d._confirms == {}
        await finish(stop, task)
    run(go)


def test_a_permission_prompt_is_never_a_reflex(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await ready(d)
        await show(d, "alerting")
        with pytest.raises(Refused):
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "settings-btn"}}]})
        assert d.session.tap_points == []
        await finish(stop, task)
    run(go)


def test_reflexes_are_off_in_a_journey_in_a_users_eyes_unless_it_allows_them(tmp_path):
    async def go():
        for allow, fires in (([], False), (["reflexes"], True)):
            d = mapped(tmp_path / str(fires))
            stop, task = await started(d)
            await ready(d, {**RATE, "do": {"tool": "tap", "target": {"id": "not-now"}}})
            await show(d, "rate")
            await d.run_tool("a", "journey", {"start": True, "goal": "g", "allow": allow})
            step = {"tool": "tap", "target": {"text": "Label of settings-btn"}}
            if fires:
                r = await d.run_tool("a", "run", {"steps": [step]})
                assert 'Reflex: closed "Rate Wayfare"' in r["text"]
            else:
                with pytest.raises(Refused) as e:  # the interruption is a finding
                    await d.run_tool("a", "run", {"steps": [step]})
                assert e.value.code == "not_found" and d.session.tap_points == []
            await finish(stop, task)
    run(go)


def test_drive_fires_a_reflex_when_the_next_target_isnt_ready_and_says_so(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        d.session.at = "home"
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "map", {"name": "home"})
        await d.run_tool("a", "tap", {"target": {"id": "settings-btn"}})
        await d.run_tool("a", "map", {"name": "settings"})
        await d.run_tool("a", "map", {"reflex": {**RATE, "at": "home"}})
        d.session.at = "covered"  # the prompt is over the page and the button is disabled under it
        d.session.goes[("covered", "not-now")] = "home"
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "drive", {"to": "settings"})
        lines = r["text"].splitlines()
        assert lines[0].startswith("Arrived at settings by 1 tap, 1 reflex in "), lines[0]
        assert LINE in lines and lines.index(LINE) < lines.index("Place: settings.")
        await finish(stop, task)
    run(go)


class Spinner(FakeSession):
    """A screen that is loading: a spinner, and the button the agent wants comes after a few reads."""

    def __init__(self, loads: int = 3, spinner: bool = True) -> None:
        super().__init__()
        self.reads, self.loads, self.spinner = 0, loads, spinner
        self.tree = [el("e9", "activityindicator", "In progress", "", rect=(150, 300, 40, 40), type="ActivityIndicator")] if spinner else []

    def _now(self):
        self.reads += 1
        ready = self.reads > self.loads
        self.items = [button("e1", "Go", "go")] if ready else list(self.tree)
        return self.items

    async def refresh(self, timeout=None):
        self._record("refresh")
        return ui.Tree(self._now(), 0, self.last_alert, self.last_keyboard)

    async def snapshot(self, keyboard=False, timeout=None):
        self._record("snapshot")
        from tapwright.session import Snapshot
        return Snapshot(ui.Tree(self._now(), 0, self.last_alert, self.last_keyboard), self.app, 3.0)


def test_while_a_loading_indicator_is_the_only_thing_on_screen_a_target_is_waited_for(tmp_path):
    async def go():
        d = make(Spinner(loads=4))
        stop, task = await started(d)
        r = await d.run_tool("a", "tap", {"target": {"id": "go"}})
        lines = r["text"].splitlines()
        assert lines[0].startswith("Tapped") and any(line.startswith("Reflex: waited ") and line.endswith(" s for loading to finish.") for line in lines)
        await finish(stop, task)
    run(go)


def test_with_no_indicator_a_missing_target_is_still_not_found_at_once(tmp_path):
    async def go():
        d = make(Spinner(loads=4, spinner=False))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "go"}})
        assert e.value.code == "not_found" and d.session.reads < 10
        await finish(stop, task)
    run(go)


def test_a_spinner_that_never_ends_gives_up_at_the_cap(tmp_path):
    async def go():
        d = make(Spinner(loads=10_000))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "go"}})
        assert e.value.code == "not_found"
        await finish(stop, task)
    run(go)


def test_the_keyboard_reflex_drags_the_form_when_a_touch_outside_the_field_closes_nothing():
    """A form that ignores a touch outside its field (seen on the phone): a small drag of its scroll area closes the
    keyboard when the form has on-drag dismissal, and the answer says which way worked."""
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False)
        d.session.tree = [*d.session.tree, el("e8", "scroll", "", "form", rect=(0, 60, 400, 440))]
        await d.run_tool("a", "look", {"screenshot": False})
        real = d.session.swipe

        async def swipe(*a, **kw):
            out = await real(*a, **kw)
            d.session.last_keyboard = False  # the drag dismissed it
            return out
        d.session.swipe = swipe
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}})
        lines = r["text"].splitlines()
        assert "Reflex: closed the keyboard (dragged the form a little)." in lines
        assert r["text"].endswith("Note: closed the keyboard first.")
        assert "dismiss_keyboard" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_then_the_keyboards_own_dismiss_key_and_the_app_is_remembered():
    async def go():
        d, stop, task = await keyboard_up(blank_tap_closes=False, dismiss_closes=True)
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}})
        assert "Reflex: closed the keyboard (the keyboard's own dismiss key)." in r["text"].splitlines()
        assert d.session.tap_points == [(60.0, 80.0), (60.0, 320.0)]  # the outside touch was tried, and did nothing
        d.session.last_keyboard = True
        d.session.tap_points.clear()
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}})
        assert d.session.tap_points == [(60.0, 320.0)]  # this form ignores outside touches: no touch there again
        assert "Reflex: closed the keyboard (the keyboard's own dismiss key)." in r["text"].splitlines()
        await finish(stop, task)
    run(go)


def test_an_outside_touch_that_works_is_not_a_reflex():
    async def go():
        d, stop, task = await keyboard_up()
        r = await d.run_tool("a", "tap", {"target": {"id": "save"}})
        assert "Reflex:" not in r["text"] and r["text"].endswith("Note: closed the keyboard first.")
        await finish(stop, task)
    run(go)
