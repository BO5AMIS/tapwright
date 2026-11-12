"""`compare_screen`, `expect.matches` and references that travel with a script (AGENT-TOOLS §14, v1.9)."""
import asyncio
import io
import json
import os

import pytest
from PIL import Image

from tapwright import batch, cli, client, compare
from tapwright.actionlog import ActionLog
from tapwright.control import Refused
from tapwright.daemon import _COMPARED, Scope
from fakes import FakeSession, button, el
from test_daemon import finish, make, started

SAVE = button("e1", "Save", "lab-save", rect=(100, 300, 100, 50))
BLOCK = (200, 600, 400, 700)  # on the 800x1600 screenshot: x 100-200, y 300-350 in points, over SAVE
CLOCK = el("e2", "text", "9:41", "clock", rect=(150, 400, 100, 20))
CLOCK_PX = (300, 800, 500, 840)


def run(go):
    asyncio.run(go())


@pytest.fixture(autouse=True)
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "references_dir", lambda: tmp_path / "refs")
    monkeypatch.setattr(batch, "scripts_dir", lambda: tmp_path / "scripts")
    return tmp_path


def show(d, *paints, size=(800, 1600)):
    """What the phone's screenshot is now: white with (box, colour) paints."""
    img = Image.new("RGB", size, (255, 255, 255))
    for box, colour in paints:
        img.paste(colour, box)
    buf = io.BytesIO()
    img.save(buf, "PNG")

    async def shot():
        d.session.calls.append("screenshot")
        return buf.getvalue()
    d.session.screenshot_png = shot


async def ready(d):
    stop, task = await started(d)
    d.session.tree = [SAVE, CLOCK]
    await d.run_tool("a", "look", {"screenshot": False})
    return stop, task


def stored(tmp_path, name, bundle="com.example.app"):
    return tmp_path / "refs" / bundle / f"{name}.png"


def test_the_first_call_saves_the_reference_and_the_next_matches(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        r = await d.run_tool("a", "compare_screen", {"name": "home"})
        assert r["text"] == 'Saved the reference "home" (600x1200 px).' and r["image"] is None
        assert Image.open(stored(tmp_path, "home")).size == (600, 1200)
        r = await d.run_tool("a", "compare_screen", {"name": "home"})
        assert r["text"] == 'Matches the reference "home": 0% changed.' and r["image"] is None
        await finish(stop, task)
    run(go)


def test_a_change_is_a_finding_with_regions_named_by_their_elements_and_a_picture(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "checkout"})
        show(d, (BLOCK, (200, 0, 0)))
        r = await d.run_tool("a", "compare_screen", {"name": "checkout"})  # not an error: a finding
        lines = r["text"].splitlines()
        assert lines[0] == 'Differs from the reference "checkout": 1.56% changed, in 1 region (points):', r["text"]
        assert lines[1] == '- @96,299 107x53: button "Save" id=lab-save'
        assert len(lines) == 2 and r["image"] and r["image_size"] == [600, 1200]
        picture = Image.open(io.BytesIO(__import__("base64").b64decode(r["image"])))
        assert picture.getpixel((5, 600))[0] > 240  # faded towards white
        await finish(stop, task)
    run(go)


def test_a_change_within_the_threshold_matches_and_says_how_much(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})
        show(d, ((0, 800, 100, 900), (0, 0, 0)))
        r = await d.run_tool("a", "compare_screen", {"name": "home", "threshold": 2})
        assert r["text"] == 'Matches the reference "home": 0.78% changed.', r["text"]
        r = await d.run_tool("a", "compare_screen", {"name": "home", "threshold": 0.5})
        assert r["text"].startswith('Differs from the reference "home": 0.78% changed, in 1 region (points):')
        await finish(stop, task)
    run(go)


def test_update_saves_the_screen_again_and_a_reference_of_another_size_is_refused(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})
        show(d, (BLOCK, (0, 0, 0)))
        r = await d.run_tool("a", "compare_screen", {"name": "home", "update": True})
        assert r["text"] == 'Saved the reference "home" (600x1200 px).'
        assert (await d.run_tool("a", "compare_screen", {"name": "home"}))["text"].startswith("Matches")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "compare_screen", {"name": "home", "width": 300})
        assert e.value.code == "bad_params"
        assert str(e.value) == ('the reference "home" is 600x1200 px and this screen is 300x600; take it at the same '
                                "width, or update: true.")
        await finish(stop, task)
    run(go)


def test_ignore_paints_out_an_element_wherever_it_is_now_and_a_missing_one_is_a_note(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})
        show(d, (CLOCK_PX, (0, 0, 0)))
        r = await d.run_tool("a", "compare_screen", {"name": "home", "ignore": [{"id": "clock"}]})
        assert r["text"] == 'Matches the reference "home": 0% changed.'
        r = await d.run_tool("a", "compare_screen", {"name": "home", "ignore": [{"x": 140, "y": 390, "width": 120,
                                                                                  "height": 40}]})
        assert r["text"] == 'Matches the reference "home": 0% changed.'
        r = await d.run_tool("a", "compare_screen", {"name": "home", "ignore": [{"id": "clock"}, {"id": "nope"}]})
        assert r["text"].splitlines()[0].startswith('Matches the reference "home"')
        assert 'Note: ignore id "nope" isn\'t on the screen; skipped.' in r["text"]
        d.session.tree = [SAVE, el("e2", "text", "9:41", "clock", rect=(10, 10, 20, 10))]  # the clock moved
        r = await d.run_tool("a", "compare_screen", {"name": "home", "ignore": [{"id": "clock"}]})
        assert r["text"].startswith("Differs")  # painted out where it is now, not where it was
        await finish(stop, task)
    run(go)


def test_the_status_bar_is_never_compared(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})
        show(d, ((0, 0, 800, 90), (0, 0, 0)))  # the top 45 pt: the clock and the battery
        r = await d.run_tool("a", "compare_screen", {"name": "home"})
        assert r["text"] == 'Matches the reference "home": 0% changed.'
        show(d, ((0, 130, 800, 190), (0, 0, 0)))  # below the bar (65 pt)
        assert (await d.run_tool("a", "compare_screen", {"name": "home"}))["text"].startswith("Differs")
        await finish(stop, task)
    run(go)


def test_the_parameters_are_checked_before_the_phone_is_touched(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        calls = len(d.session.calls)
        for args, words in (({"name": "../x"}, "name the reference"), ({"name": ""}, "name the reference"),
                            ({"name": "a" * 62}, "name the reference"), ({"name": "home", "threshold": 101}, "0 to 100"),
                            ({"name": "home", "threshold": True}, "0 to 100"),
                            ({"name": "home", "update": "yes"}, "true or false"),
                            ({"name": "home", "ignore": []}, "non-empty list"),
                            ({"name": "home", "ignore": [{"x": 1, "y": 2}]}, "has no size"),
                            ({"name": "home", "ignore": [{"x": 1, "y": 2, "width": 0, "height": 5}]}, "x, y, width and height"),
                            ({"name": "home", "width": 50}, "width must be"), ({}, "needs name")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "compare_screen", args)
            assert e.value.code == "bad_params" and words in str(e.value), (args, str(e.value))
        # heartbeats (`locked`) are the daemon's own and may land in between on any version (seen on 3.10)
        assert [c for c in d.session.calls[calls:] if c not in ("locked", "ensure", "reset", "close")] == []
        await finish(stop, task)
    run(go)


def test_in_personal_mode_only_a_given_apps_screen_is_compared_or_saved(tmp_path):
    async def go():
        s = FakeSession()
        s.app_names = {"com.example.app": "Example", "com.example.mail": "Mail box"}
        d = make(s)
        stop, task = await ready(d)
        show(d)
        mine = Scope(("com.example.app",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "compare_screen", {"name": "home"}, scope=Scope(()))
        assert e.value.code == "not_allowed" and str(e.value).startswith("no_apps")
        s.foreground = "com.example.app"
        assert (await d.run_tool("a", "compare_screen", {"name": "home"}, scope=mine))["text"].startswith("Saved")
        s.foreground, s.app = "com.example.mail", ("Mail box", "com.example.mail")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "compare_screen", {"name": "mail"}, scope=mine)
        assert e.value.code == "not_allowed" and "outside_apps: " in str(e.value)
        assert not stored(tmp_path, "mail", "com.example.mail").exists()
        await finish(stop, task)
    run(go)


def test_an_app_that_leaves_while_the_picture_is_taken_saves_nothing(tmp_path):
    """Review 2026-11-12 (M1): the question before the call can't see an app that changes during it, so the front app
    is asked again once the picture is taken."""
    async def go():
        s = FakeSession()
        s.app_names = {"com.example.app": "Example", "com.example.mail": "Mail box"}
        d = make(s)
        stop, task = await ready(d)
        show(d)
        taken = d.session.screenshot_png

        async def shot_then_leave():
            png = await taken()
            s.foreground = "com.example.mail"  # the picture may be of Mail by now
            return png
        s.foreground, d.session.screenshot_png = "com.example.app", shot_then_leave
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "compare_screen", {"name": "home"}, scope=Scope(("com.example.app",)))
        assert e.value.code == "not_allowed" and "outside_apps: com.example.mail" in str(e.value)
        assert not stored(tmp_path, "home").exists() and not stored(tmp_path, "home", "com.example.mail").exists()
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("name", ["home\n", "../up", ".hidden", "a/b", "x" * 62])
def test_names_that_arent_plain_are_bad_params(name):
    with pytest.raises(Refused) as e:
        compare.check_params({"name": name})
    assert e.value.code == "bad_params"


def test_the_action_log_has_the_name_and_the_percentage_and_no_picture(tmp_path):
    async def go():
        d = make(actions=ActionLog(tmp_path / "log"))
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})
        show(d, (BLOCK, (0, 0, 0)))
        await d.run_tool("a", "compare_screen", {"name": "home"})
        await finish(stop, task)
        lines = [json.loads(x) for f in (tmp_path / "log").glob("actions-*.jsonl") for x in f.read_text("utf-8").splitlines()]
        entries = [x for x in lines if x["tool"] == "compare_screen"]
        assert [(x["name"], x.get("percent"), x["ok"]) for x in entries] == [("home", None, True), ("home", 1.56, True)]
        assert "image" not in json.dumps(entries)
    run(go)


# ---- in run and scripts ----
def test_a_compare_screen_step_checks_and_a_difference_stops_the_batch(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})
        r = await d.run_tool("a", "run", {"steps": [{"tool": "compare_screen", "name": "home"}]})
        assert r["text"].startswith("Ran 1 of 1 steps")
        show(d, (BLOCK, (0, 0, 0)))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1},
                                                    {"tool": "compare_screen", "name": "home"}]})
        assert e.value.code == "expect_failed"
        assert str(e.value).splitlines()[0] == ('Ran 1 of 2 steps; stopped at step 2 (compare_screen "home"): 1.56% changed '
                                                'from the reference "home" (threshold 0.5%), in 1 region (points): '
                                                "@96,299 107x53.")
        r = await d.run_tool("a", "run", {"steps": [{"tool": "compare_screen", "name": "home", "threshold": 5}]})
        assert r["text"].startswith("Ran 1 of 1 steps")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "compare_screen", "name": "never"}]})
        assert e.value.code == "expect_failed" and str(e.value).splitlines()[0] == (
            'Ran 0 of 1 steps; stopped at step 1 (compare_screen "never"): no reference "never" yet; save one with '
            'compare_screen { name: "never" }.')
        assert not stored(tmp_path, "never").exists()  # a step never saves
        await finish(stop, task)
    run(go)


def test_a_step_cant_update_and_its_parameters_are_checked_up_front(tmp_path):
    for steps, words in (([{"tool": "compare_screen", "name": "home", "update": True}], "never saves"),
                         ([{"tool": "compare_screen"}], "needs a name"),
                         ([{"tool": "compare_screen", "name": "a b"}], "step 1: name the reference"),
                         ([{"tool": "tap", "expect": {"matches": "a b"}}], "expect's matches: name the reference"),
                         ([{"tool": "tap", "expect": {"matches": "x", "threshold": 120}}], "0 to 100"),
                         ([{"tool": "tap", "expect": {"text": "a", "threshold": 1}}], "go with matches"),
                         ([{"tool": "tap", "expect": {"value": "on", "matches": "x"}}], "value goes with"),
                         ([{"tool": "tap", "expect": {"text": "a", "id": "b", "matches": "x"}}], "exactly one of text")):
        with pytest.raises(Refused) as e:
            batch.check_steps(steps)
        assert e.value.code == "bad_params" and words in str(e.value), (steps, str(e.value))
    with pytest.raises(Refused) as e:
        batch.check_steps([{"tool": "compare_screen", "name": "x", "ignore": [{"ref": "e1"}]}], script=True)
    assert "can't use refs in ignore" in str(e.value)
    batch.check_steps([{"tool": "tap", "expect": {"matches": "x"}}, {"tool": "tap", "expect": {"text": "Saved", "matches": "y",
                                                                                               "threshold": 1, "ignore": [{"id": "c"}]}}])
    assert batch.summary({"tool": "compare_screen", "name": "home"}) == 'compare_screen "home"'


def test_matches_is_checked_once_after_the_action_and_after_the_text_held(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})
        r = await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "lab-save"},
                                                     "expect": {"matches": "home"}}]})
        assert r["text"].startswith("Ran 1 of 1 steps")
        show(d, (BLOCK, (0, 0, 0)))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "lab-save"},
                                                     "expect": {"matches": "home"}}]})
        assert e.value.code == "expect_failed"
        assert str(e.value).splitlines()[0] == ('Ran 0 of 1 steps; stopped at step 1 (tap id=lab-save): its action ran, but '
                                                '1.56% changed from the reference "home" (threshold 0.5%), in 1 region '
                                                "(points): @96,299 107x53.")
        with pytest.raises(Refused) as e:  # the text is waited for first: its own reason comes first
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "lab-save"},
                                                     "expect": {"text": "Saved", "timeout": 0.5, "matches": "home"}}]})
        assert "text \"Saved\" didn't appear within 0.5 s." in str(e.value) and "changed from" not in str(e.value)
        d.session.tree = [SAVE, CLOCK, el("e3", "text", "Saved", rect=(10, 300, 100, 20))]
        with pytest.raises(Refused) as e:  # the text held; then the picture is looked at
            await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1,
                                                     "expect": {"text": "Saved", "matches": "home"}}]})
        assert "changed from the reference" in str(e.value)
        r = await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1, "expect": {
            "matches": "home", "threshold": 10, "ignore": [{"id": "nope"}]}}]})
        assert "Note (step 1): ignore id \"nope\" isn't on the screen; skipped." in r["text"]
        await finish(stop, task)
    run(go)


def test_matches_with_no_reference_says_so(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1, "expect": {"matches": "x"}}]})
        assert str(e.value).splitlines()[0] == ('Ran 0 of 1 steps; stopped at step 1 (wait 0.1): its action ran, but no '
                                                'reference "x" yet; save one with compare_screen { name: "x" }.')
        await finish(stop, task)
    run(go)


def test_a_batch_in_personal_mode_compares_only_a_given_apps_screen(tmp_path):
    async def go():
        s = FakeSession()
        s.app_names = {"com.example.app": "Example", "com.example.mail": "Mail box"}
        d = make(s)
        stop, task = await ready(d)
        show(d)
        mine = Scope(("com.example.app",))
        s.foreground = "com.example.app"
        await d.run_tool("a", "compare_screen", {"name": "home"}, scope=mine)
        r = await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1, "expect": {"matches": "home"}}]},
                             scope=mine)
        assert r["text"].startswith("Ran 1 of 1 steps")
        s.foreground, s.app = "com.example.mail", ("Mail box", "com.example.mail")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1, "expect": {"matches": "home"}}]},
                             scope=mine)
        assert e.value.code == "not_allowed"
        await finish(stop, task)
    run(go)


# ---- record ----
def test_record_folds_a_compare_into_the_action_before_it_and_copies_the_references(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        path = tmp_path / "flows" / "buy.json"
        await d.run_tool("a", "record", {"start": True, "name": "buy"})
        await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False})
        await d.run_tool("a", "compare_screen", {"name": "after-tap", "ignore": [{"ref": "e2"}], "threshold": 1.5})
        await d.run_tool("a", "compare_screen", {"name": "second"})  # the step before it has a compare already: its own
        await d.run_tool("a", "wait_for", {"id": "lab-save", "look": False})  # after a compare step: no expect to add
        await d.run_tool("a", "tap", {"target": {"id": "lab-save"}, "look": False})
        await d.run_tool("a", "wait_for", {"text": "Save", "look": False})
        await d.run_tool("a", "compare_screen", {"name": "after-text", "width": 600})
        r = await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        folder = tmp_path / "flows" / "buy.references"
        assert r["text"] == f"Saved 3 steps to {path}, and 3 reference pictures to {folder}.", r["text"]
        steps = json.loads(path.read_text("utf-8"))["steps"]
        assert steps == [
            {"tool": "tap", "target": {"id": "lab-save"}, "expect": {"matches": "after-tap", "threshold": 1.5,
                                                                      "ignore": [{"id": "clock"}]}},
            {"tool": "compare_screen", "name": "second"},
            {"tool": "tap", "target": {"id": "lab-save"}, "expect": {"text": "Save", "matches": "after-text"}},
        ]
        assert sorted(p.name for p in folder.iterdir()) == ["after-tap.png", "after-text.png", "second.png"]
        await finish(stop, task)
    run(go)


def test_a_compare_with_nothing_before_it_is_a_step_of_its_own(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "record", {"start": True, "name": "solo"})
        await d.run_tool("a", "compare_screen", {"name": "first", "width": 300})
        path = tmp_path / "solo.json"
        r = await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        assert r["text"] == f"Saved 1 steps to {path}, and 1 reference picture to {tmp_path / 'solo.references'}."
        assert json.loads(path.read_text("utf-8"))["steps"] == [{"tool": "compare_screen", "name": "first", "width": 300}]
        await finish(stop, task)
    run(go)


def test_record_cant_save_an_ignored_ref_that_has_no_id_label_or_text(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        bare = el("e2", "other", "", "", rect=(150, 400, 100, 20))
        d.session.tree = [SAVE, bare]
        await d.run_tool("a", "look", {"screenshot": False})
        show(d)
        await d.run_tool("a", "record", {"start": True})
        await d.run_tool("a", "tap", {"target": {"id": "lab-save"}, "look": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "compare_screen", {"name": "x", "ignore": [{"ref": "e2"}]})
        assert e.value.code == "bad_params" and "can't record ignore e2" in str(e.value)
        assert not stored(tmp_path, "x").exists()  # refused before anything was taken
        await d.run_tool("a", "compare_screen", {"name": "y", "ignore": [{"x": 0, "y": 0, "width": 10, "height": 10}]})
        await finish(stop, task)
    run(go)


# ---- replay ----
def write_script(folder, name, steps):
    path = folder / f"{name}.json"
    path.write_text(json.dumps({"format": "agent-tools-script", "version": 1, "name": name, "app": None, "fresh": False,
                                "steps": steps}), "utf-8")
    return path


def test_a_replay_looks_in_the_scripts_references_folder_before_the_store(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})  # the store: a white screen
        show(d, (BLOCK, (0, 0, 0)))
        script = write_script(tmp_path, "flow", [{"tool": "compare_screen", "name": "home"}])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "replay", {"path": str(script)})
        assert e.value.code == "expect_failed"
        # the script's own copy is the screen with the block: now that matches, whatever the store holds
        shot = Image.new("RGB", (800, 1600), (255, 255, 255))
        shot.paste((0, 0, 0), BLOCK)
        buf = io.BytesIO()
        shot.save(buf, "PNG")
        compare.save(tmp_path / "flow.references" / "home.png", compare.fit(buf.getvalue(), 600))
        r = await d.run_tool("a", "replay", {"path": str(script)})
        assert r["text"].startswith("Ran 1 of 1 steps")
        await finish(stop, task)
    run(go)


def test_a_failed_check_in_a_replay_writes_its_picture_beside_the_script(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})
        show(d, (BLOCK, (0, 0, 0)))
        script = write_script(tmp_path, "flow", [{"tool": "wait", "seconds": 0.1, "expect": {"matches": "home"}}])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "replay", {"path": str(script)})
        diff = tmp_path / "flow.diff" / "home.png"
        first = str(e.value).splitlines()[0]
        assert first.endswith(f"@96,299 107x53. (diff: {diff})"), first
        assert Image.open(diff).size == (600, 1200)
        with pytest.raises(Refused) as e:  # a run has no script: no picture is written
            await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1, "expect": {"matches": "home"}}]})
        assert "(diff:" not in str(e.value)
        await finish(stop, task)
    run(go)


def test_update_references_makes_the_scripts_own_references_and_every_check_passes(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d, (BLOCK, (0, 0, 0)))
        script = write_script(tmp_path, "flow", [{"tool": "wait", "seconds": 0.1, "expect": {"matches": "home"}},
                                                 {"tool": "compare_screen", "name": "second"}])
        r = await d.run_tool("a", "replay", {"path": str(script), "update_references": True})
        assert r["text"].startswith("Ran 2 of 2 steps")
        own = tmp_path / "flow.references"
        assert sorted(p.name for p in own.iterdir()) == ["home.png", "second.png"]
        assert not stored(tmp_path, "home").exists()
        r = await d.run_tool("a", "replay", {"path": str(script)})  # and now they are the ones compared with
        assert r["text"].startswith("Ran 2 of 2 steps")
        show(d)
        with pytest.raises(Refused):
            await d.run_tool("a", "replay", {"path": str(script)})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "wait", "seconds": 0.1}], "update_references": True})
        assert e.value.code == "bad_params"
        await finish(stop, task)
    run(go)


# ---- the command line ----
def test_replay_takes_update_references_and_a_junit_message_ends_with_the_diff(tmp_path, monkeypatch):
    import argparse
    from xml.etree import ElementTree as ET
    sent = []
    diff = tmp_path / "flow.diff" / "home.png"

    def call(tool, args, client_id, timeout=120.0):
        sent.append(args)
        if "update_references" not in args:
            raise client.DaemonError("expect_failed", f'Ran 0 of 1 steps; stopped at step 1 (compare_screen "home"): 3% '
                                                      f"changed from the reference \"home\" (threshold 0.5%), in 1 region "
                                                      f"(points): @1,2 3x4. (diff: {diff})")
        return {"text": "Ran 1 of 1 steps in 1.0 s."}

    monkeypatch.setattr(client, "call", call)
    monkeypatch.setattr(client, "set_scope", lambda apps, dedicated=False: None)
    script = write_script(tmp_path, "flow", [{"tool": "compare_screen", "name": "home"}])
    out = tmp_path / "junit.xml"
    a = argparse.Namespace(files=[str(script)], junit=str(out), apps="com.example.app", dedicated=False)
    assert cli.replay(a) == 1
    failure = ET.parse(out).getroot().find("testcase/failure")
    assert failure.get("message").endswith(f"(diff: {diff})")
    a = argparse.Namespace(files=[str(script)], junit=None, apps="com.example.app", dedicated=False,
                           update_references=True)
    assert cli.replay(a) == 0
    assert sent == [{"path": str(script)}, {"path": str(script), "update_references": True}]
    seen = []
    monkeypatch.setattr(cli, "replay", lambda args: seen.append(args) or 0)
    monkeypatch.setattr("sys.argv", ["tapwright", "replay", "x.json", "--apps", "a", "--update-references"])
    assert cli.main() == 0 and seen[0].update_references is True
    monkeypatch.setattr("sys.argv", ["tapwright", "replay", "x.json", "--apps", "a"])
    assert cli.main() == 0 and seen[1].update_references is False


# ---- the review of PR #3 ----
def link(path, target, directory=False):
    try:
        os.symlink(target, path, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("this system doesn't let the tests make symlinks")


def test_a_reference_wider_than_any_screen_is_refused_before_the_screen_is_resized(tmp_path, monkeypatch):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        folder = tmp_path / "flow.references"
        folder.mkdir()
        Image.new("RGB", (100_000, 1)).save(folder / "home.png")  # 100,000 px: under the size limit, far too wide
        script = write_script(tmp_path, "flow", [{"tool": "compare_screen", "name": "home"}])
        seen = []
        real = Image.Image.resize

        def resize(self, size, *args, **kwargs):
            seen.append(tuple(size))
            assert size[0] * size[1] <= compare.MAX_PIXELS, f"asked to allocate {size[0]}x{size[1]}"
            return real(self, size, *args, **kwargs)
        monkeypatch.setattr(Image.Image, "resize", resize)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "replay", {"path": str(script)})
        assert e.value.code == "bad_params" and "100000 px wide" in str(e.value), str(e.value)
        await finish(stop, task)
    run(go)


def test_a_failed_check_replaces_a_link_at_its_picture_and_never_writes_through_it(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})
        show(d, (BLOCK, (0, 0, 0)))
        outside = tmp_path / "important.txt"
        outside.write_text("keep")
        (tmp_path / "flow.diff").mkdir()
        link(tmp_path / "flow.diff" / "home.png", outside)
        script = write_script(tmp_path, "flow", [{"tool": "compare_screen", "name": "home", "threshold": 0}])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "replay", {"path": str(script)})
        assert outside.read_text() == "keep"
        assert "(diff:" in str(e.value) and Image.open(tmp_path / "flow.diff" / "home.png").size == (600, 1200)
        await finish(stop, task)
    run(go)


def test_a_diff_folder_that_is_a_link_gets_no_picture(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})
        show(d, (BLOCK, (0, 0, 0)))
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        link(tmp_path / "flow.diff", elsewhere, directory=True)
        script = write_script(tmp_path, "flow", [{"tool": "compare_screen", "name": "home"}])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "replay", {"path": str(script)})
        assert e.value.code == "expect_failed" and "(diff:" not in str(e.value)
        assert list(elsewhere.iterdir()) == []
        await finish(stop, task)
    run(go)


def test_update_references_never_writes_through_a_link(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        outside = tmp_path / "important.txt"
        outside.write_text("keep")
        folder = tmp_path / "flow.references"
        folder.mkdir()
        link(folder / "home.png.part", outside)
        link(folder / "home.png", outside)
        script = write_script(tmp_path, "flow", [{"tool": "compare_screen", "name": "home"}])
        r = await d.run_tool("a", "replay", {"path": str(script), "update_references": True})
        assert r["text"].startswith("Ran 1 of 1 steps") and outside.read_text() == "keep"
        assert Image.open(folder / "home.png").size == (600, 1200)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        link(tmp_path / "other.references", elsewhere, directory=True)
        script = write_script(tmp_path, "other", [{"tool": "compare_screen", "name": "home"}])
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "replay", {"path": str(script), "update_references": True})
        assert "link" in str(e.value) and list(elsewhere.iterdir()) == []
        await finish(stop, task)
    run(go)


def test_a_run_while_recording_keeps_its_visual_checks_and_their_references(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})  # the reference, saved before recording
        path = tmp_path / "flows" / "flow.json"
        await d.run_tool("a", "record", {"start": True, "name": "flow"})
        r = await d.run_tool("a", "run", {"steps": [
            {"tool": "tap", "target": {"id": "lab-save"},
             "expect": {"matches": "home", "threshold": 2, "ignore": [{"ref": "e2"}]}},
            {"tool": "compare_screen", "name": "home", "threshold": 1}]})
        assert r["text"].startswith("Ran 2 of 2 steps")
        r = await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        folder = tmp_path / "flows" / "flow.references"
        assert r["text"].startswith(f"Saved 2 steps to {path}, and 1 reference picture to {folder}"), r["text"]
        data = json.loads(path.read_text("utf-8"))
        assert data["steps"] == [
            {"tool": "tap", "target": {"id": "lab-save"},
             "expect": {"matches": "home", "threshold": 2, "ignore": [{"id": "clock"}]}},
            {"tool": "compare_screen", "name": "home", "threshold": 1}]
        assert [p.name for p in folder.iterdir()] == ["home.png"]
        data["fresh"] = False
        path.write_text(json.dumps(data), "utf-8")
        r = await d.run_tool("a", "replay", {"path": str(path)})  # the checks are really made
        assert r["text"].startswith("Ran 2 of 2 steps")
        show(d, (BLOCK, (0, 0, 0)))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "replay", {"path": str(path)})
        # the first check (threshold 2) holds with the block, the second step's compare_screen (threshold 1) doesn't
        assert e.value.code == "expect_failed" and "stopped at step 2" in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_check_that_did_not_hold_in_a_recorded_run_is_not_saved(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})
        show(d, (BLOCK, (0, 0, 0)))
        await d.run_tool("a", "record", {"start": True, "name": "flow"})
        with pytest.raises(Refused):
            await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "lab-save"}},
                                                    {"tool": "compare_screen", "name": "home"}]})
        path = tmp_path / "flow.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        assert json.loads(path.read_text("utf-8"))["steps"] == [{"tool": "tap", "target": {"id": "lab-save"}}]
        await finish(stop, task)
    run(go)


def test_a_recorded_width_is_a_whole_number_the_script_loader_accepts(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "record", {"start": True, "name": "wide"})
        await d.run_tool("a", "compare_screen", {"name": "first", "width": 300.0})
        path = tmp_path / "wide.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        step = json.loads(path.read_text("utf-8"))["steps"][0]
        assert step["width"] == 300 and isinstance(step["width"], int)
        assert batch.load_script(str(path))["steps"] == [step]
        await finish(stop, task)
    run(go)


def test_a_recording_cant_compare_one_name_in_two_apps(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "record", {"start": True, "name": "two"})
        await d.run_tool("a", "compare_screen", {"name": "home"})
        d.session.app = ("Other", "com.example.other")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "compare_screen", {"name": "home"})
        assert e.value.code == "bad_params" and "home" in str(e.value) and "com.example.app" in str(e.value)
        await d.run_tool("a", "compare_screen", {"name": "elsewhere"})  # another name is fine
        d.session.app = ("Example", "com.example.app")
        await d.run_tool("a", "compare_screen", {"name": "home"})  # and the same name in the same app again
        path = tmp_path / "two.json"
        r = await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        assert [s["name"] for s in json.loads(path.read_text("utf-8"))["steps"]] == ["home", "elsewhere", "home"]
        assert sorted(p.name for p in (tmp_path / "two.references").iterdir()) == ["elsewhere.png", "home.png"], r["text"]
        await finish(stop, task)
    run(go)


def test_the_bundle_a_recording_copies_from_is_its_own_calls_not_the_daemons(tmp_path, monkeypatch):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        loop, other, real_fit = asyncio.get_running_loop(), {"done": False}, compare.fit

        async def theirs():
            _COMPARED.set(None)  # (a task started from this worker thread inherits its context; another client's call doesn't)
            d.session.app = ("Other", "com.example.other")
            await d.run_tool("b", "compare_screen", {"name": "theirs"})
            d.session.app = ("Example", "com.example.app")

        def fit(png, width):
            if not other["done"]:  # in the middle of a's comparison (the picture is being scaled), b compares elsewhere
                other["done"] = True
                asyncio.run_coroutine_threadsafe(theirs(), loop).result(timeout=20)
            return real_fit(png, width)
        monkeypatch.setattr(compare, "fit", fit)
        await d.run_tool("a", "record", {"start": True, "name": "mine"})
        await d.run_tool("a", "compare_screen", {"name": "mine"})
        path = tmp_path / "mine.json"
        r = await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        assert r["text"] == f"Saved 1 steps to {path}, and 1 reference picture to {tmp_path / 'mine.references'}.", r["text"]
        assert not hasattr(d, "_compared_in")
        await finish(stop, task)
    run(go)


def test_a_references_folder_that_is_a_link_gets_no_reference_saved(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (tmp_path / "refs").mkdir()
        link(tmp_path / "refs" / "com.example.app", elsewhere, directory=True)  # the app's folder in the store
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "compare_screen", {"name": "home"})
        assert "link" in str(e.value) and list(elsewhere.iterdir()) == []
        await finish(stop, task)
    run(go)


def test_recording_a_replay_copies_the_reference_the_replay_compared_with(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "compare_screen", {"name": "home"})  # the store's "home": a white screen
        show(d, (BLOCK, (0, 0, 0)))
        shot = Image.new("RGB", (800, 1600), (255, 255, 255))
        shot.paste((0, 0, 0), BLOCK)
        buf = io.BytesIO()
        shot.save(buf, "PNG")
        mine = compare.fit(buf.getvalue(), 600)
        compare.save(tmp_path / "old.references" / "home.png", mine)  # old.json's own "home": the screen with the block
        compare.save(tmp_path / "old.references" / "second.png", mine)  # and one the store has no copy of
        old = write_script(tmp_path, "old", [{"tool": "compare_screen", "name": "home"},
                                             {"tool": "wait", "seconds": 0.1, "expect": {"matches": "second"}}])
        await d.run_tool("a", "record", {"start": True, "name": "new"})
        r = await d.run_tool("a", "replay", {"path": str(old)})  # the checks hold against the script's own pictures
        assert r["text"].startswith("Ran 2 of 2 steps"), r["text"]
        new = tmp_path / "new.json"
        r = await d.run_tool("a", "record", {"stop": True, "path": str(new)})
        folder = tmp_path / "new.references"
        assert r["text"].startswith(f"Saved 2 steps to {new}, and 2 reference pictures to {folder}"), r["text"]
        assert sorted(p.name for p in folder.iterdir()) == ["home.png", "second.png"]
        assert (folder / "home.png").read_bytes() == (tmp_path / "old.references" / "home.png").read_bytes()
        assert (folder / "home.png").read_bytes() != stored(tmp_path, "home").read_bytes()  # not the store's
        r = await d.run_tool("a", "replay", {"path": str(new)})  # and the new script's checks hold on their own
        assert r["text"].startswith("Ran 2 of 2 steps"), r["text"]
        await finish(stop, task)
    run(go)


def test_recording_a_replay_that_updates_its_references_copies_the_ones_it_made(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d, (BLOCK, (0, 0, 0)))
        old = write_script(tmp_path, "old", [{"tool": "compare_screen", "name": "home"}])
        await d.run_tool("a", "record", {"start": True, "name": "new"})
        await d.run_tool("a", "replay", {"path": str(old), "update_references": True})
        new = tmp_path / "new.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(new)})
        made = tmp_path / "old.references" / "home.png"
        assert made.is_file() and (tmp_path / "new.references" / "home.png").read_bytes() == made.read_bytes()
        await finish(stop, task)
    run(go)


def _own_picture(tmp_path, folder, name, *paints):
    """A script's own reference, `name` in `folder`.references: the white screen with the paints, as the replay sees it."""
    shot = Image.new("RGB", (800, 1600), (255, 255, 255))
    for box, colour in paints:
        shot.paste(colour, box)
    buf = io.BytesIO()
    shot.save(buf, "PNG")
    compare.save(tmp_path / f"{folder}.references" / f"{name}.png", compare.fit(buf.getvalue(), 600))


def test_a_recording_refuses_one_name_compared_against_two_different_pictures(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        _own_picture(tmp_path, "first", "home")
        _own_picture(tmp_path, "second", "home", (BLOCK, (0, 0, 0)))  # the same name, another picture, in the same app
        first = write_script(tmp_path, "first", [{"tool": "compare_screen", "name": "home"}])
        second = write_script(tmp_path, "second", [{"tool": "compare_screen", "name": "home"}])
        await d.run_tool("a", "record", {"start": True, "name": "both"})
        r = await d.run_tool("a", "replay", {"path": str(first)})
        assert r["text"].startswith("Ran 1 of 1 steps"), r["text"]
        show(d, (BLOCK, (0, 0, 0)))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "replay", {"path": str(second)})
        assert e.value.code == "bad_params"
        assert 'record: "home" was already compared against another picture in this recording; give this check another name.' \
            in str(e.value)
        path = tmp_path / "both.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        assert len(json.loads(path.read_text("utf-8"))["steps"]) == 1  # the second check was not recorded
        assert (tmp_path / "both.references" / "home.png").read_bytes() == (tmp_path / "first.references" / "home.png").read_bytes()
        await finish(stop, task)
    run(go)


def test_a_recording_refuses_a_reference_updated_between_two_checks_of_its_name(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "record", {"start": True, "name": "moved"})
        await d.run_tool("a", "compare_screen", {"name": "home"})  # the store's "home": a white screen
        show(d, (BLOCK, (0, 0, 0)))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "compare_screen", {"name": "home", "update": True})  # now another picture
        assert e.value.code == "bad_params"
        assert str(e.value) == 'record: "home" was already compared against another picture in this recording; give this check another name.'
        await d.run_tool("a", "compare_screen", {"name": "home-2"})  # another name is fine
        await finish(stop, task)
    run(go)


def test_a_refused_update_in_a_recording_changes_nothing_on_disk(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "record", {"start": True, "name": "kept"})
        await d.run_tool("a", "compare_screen", {"name": "home"})  # picture A: the store's "home", a white screen
        kept = stored(tmp_path, "home")
        original = kept.read_bytes()
        show(d, (BLOCK, (0, 0, 0)))  # the screen changes: picture B
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "compare_screen", {"name": "home", "update": True})
        assert e.value.code == "bad_params"
        assert kept.read_bytes() == original  # the refusal wrote nothing: the store still holds A
        assert [p.name for p in kept.parent.iterdir()] == ["home.png"]  # and left no temporary file
        path = tmp_path / "kept.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        assert (tmp_path / "kept.references" / "home.png").read_bytes() == original  # the script exports A
        assert kept.read_bytes() == original
        await finish(stop, task)
    run(go)


def test_a_recording_allows_an_update_that_writes_the_same_picture(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "record", {"start": True, "name": "again"})
        await d.run_tool("a", "compare_screen", {"name": "home"})
        await d.run_tool("a", "compare_screen", {"name": "home", "update": True})  # the screen is the same: the same bytes
        await finish(stop, task)
    run(go)


def test_a_recording_allows_one_name_compared_twice_against_the_same_picture(tmp_path):
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)
        await d.run_tool("a", "record", {"start": True, "name": "same"})
        await d.run_tool("a", "compare_screen", {"name": "home"})
        await d.run_tool("a", "compare_screen", {"name": "home"})  # the same file, unchanged
        _own_picture(tmp_path, "copy", "home")  # another file with the same picture: not a conflict either
        copy = write_script(tmp_path, "copy", [{"tool": "compare_screen", "name": "home"}])
        r = await d.run_tool("a", "replay", {"path": str(copy)})
        assert r["text"].startswith("Ran 1 of 1 steps"), r["text"]
        path = tmp_path / "same.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        assert [s["name"] for s in json.loads(path.read_text("utf-8"))["steps"]] == ["home", "home", "home"]
        await finish(stop, task)
    run(go)


def test_the_same_name_in_another_app_is_refused_before_its_picture_is_saved(tmp_path):
    """A replay with update_references compares "home" in app A, then in app B: the second is refused before it
    writes, so A's picture stays the baseline on disk (Codex, PR #3 cycle 5)."""
    from PIL import Image
    from tapwright import daemon as daemon_module
    from tapwright.control import Refused
    first = tmp_path / "home.png"
    Image.new("RGB", (4, 4), (255, 0, 0)).save(first)
    before = first.read_bytes()
    token = daemon_module._BASELINES.set([("com.example.a", "home", first, None)])
    try:
        with pytest.raises(Refused) as e:
            daemon_module.Daemon._refuse_changed_baseline("home", "com.example.b", Image.new("RGB", (4, 4), (0, 0, 255)))
        assert e.value.code == "bad_params" and "already compares \"home\" in com.example.a" in str(e.value)
        daemon_module.Daemon._refuse_changed_baseline("other", "com.example.b", Image.new("RGB", (4, 4), (0, 0, 255)))
    finally:
        daemon_module._BASELINES.reset(token)
    assert first.read_bytes() == before


def test_a_replay_updating_one_name_in_two_apps_keeps_the_first_apps_picture(tmp_path):
    """Codex's case (PR #3 cycle 5): while recording, a replay with update_references compares "home" in app A, then
    in app B. B's check is refused before it writes, so the script's own reference and what `record stop` exports
    stay A's picture, byte for byte."""
    async def go():
        d = make()
        stop, task = await ready(d)
        show(d)  # picture A
        flow = write_script(tmp_path, "flow", [{"tool": "compare_screen", "name": "home"}])
        await d.run_tool("a", "record", {"start": True, "name": "new"})
        await d.run_tool("a", "replay", {"path": str(flow), "update_references": True})
        own = tmp_path / "flow.references" / "home.png"
        original = own.read_bytes()
        d.session.app = ("Other", "com.example.other")
        show(d, (BLOCK, (0, 0, 0)))  # picture B, in another app
        with pytest.raises(Refused):
            await d.run_tool("a", "replay", {"path": str(flow), "update_references": True})
        assert own.read_bytes() == original  # refused before anything was written
        d.session.app = ("Example", "com.example.app")
        path = tmp_path / "new.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        assert (tmp_path / "new.references" / "home.png").read_bytes() == original
        assert own.read_bytes() == original
        await finish(stop, task)
    run(go)
