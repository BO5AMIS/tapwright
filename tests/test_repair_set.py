"""AGENT-TOOLS §26.8 on fakes: 20 recorded scripts of a trip-planning app (Wayfare-like, synthetic) replayed after deliberate changes
to its screens: 5 renamed testIDs, 5 moved buttons, 5 new screens in the way and 5 real bugs where an `expect` fails. At least 12
of the 15 repairable breaks are repaired correctly and 0 wrong repairs are applied; all 5 real bugs are reported and none is
repaired. (The real set, on Wayfare, is a phone check.)"""
import json

import pytest
from fakes import button, el
from mapfakes import screen
from test_daemon_repair import BUNDLE, Flow, mapped, phone, repair_lines, replay, script, tap, title
from test_daemon import finish, run, started

from tapwright.control import Refused

HOME = [title("Home"), button("h1", "Go", "go", rect=(10, 600, 100, 40))]

# (old id, the new id) of the renamed testIDs: each is a rename the id_stem part is meant to follow
RENAMES = [("login-btn", "login-button"), ("save-trip-btn", "save-trip-button"), ("add-day-button", "add-day-btn"),
           ("tripRow", "trip-row"), ("search-field", "search-input")]


@pytest.mark.parametrize("old,new", RENAMES)
def test_a_renamed_testid_is_repaired_to_the_right_element(tmp_path, old, new):
    async def go():
        tree = [title("Start"), button("e1", "Do it", new, rect=(10, 600, 100, 40)),
                button("e2", "Other thing", "unrelated-link", rect=(10, 300, 100, 40))]
        d = phone(tmp_path, Flow(tree, {new: HOME}))
        stop, task = await started(d)
        r = await replay(d, script(tmp_path, [tap({"id": old}, expect={"text": "Home"})]), "apply")
        assert "with 1 repair" in r["text"]
        fixed = json.loads((tmp_path / "sign_in.repaired.json").read_text("utf-8"))
        assert fixed["steps"][0]["target"] == {"id": new}  # the right element, never the other
        await finish(stop, task)
    run(go)


# (the old words, the new label of the same button) for the moved buttons: seen tells which button it is
MOVED = [("Skip", "Not now", "c2r3"), ("Next", "Continue", "c3r3"), ("Add trip", "New trip", "c1r1"),
         ("Done", "Finish", "c3r1"), ("Share", "Invite friends", "c2r2")]
BUCKET_RECT = {"c1r1": (10, 60, 100, 40), "c2r1": (150, 60, 100, 40), "c3r1": (290, 60, 100, 40), "c1r2": (10, 380, 100, 40),
               "c2r2": (150, 380, 100, 40), "c3r2": (290, 380, 100, 40), "c1r3": (10, 700, 100, 40), "c2r3": (150, 700, 100, 40),
               "c3r3": (290, 700, 100, 40)}


@pytest.mark.parametrize("old,new,bucket", MOVED)
def test_a_moved_and_relabelled_button_is_found_by_its_facts(tmp_path, old, new, bucket):
    async def go():
        far = "c1r3" if bucket != "c1r3" else "c3r1"
        tree = [title("Start"), button("e1", new, "x-" + new.split()[0].lower(), rect=BUCKET_RECT[bucket]),
                button("e2", "Help centre", "help", rect=BUCKET_RECT[far])]
        d = phone(tmp_path, Flow(tree, {"x-" + new.split()[0].lower(): HOME}))
        stop, task = await started(d)
        seen = {"role": "button", "bucket": bucket, "component": "Button"}
        r = await replay(d, script(tmp_path, [tap({"text": old}, seen=seen, expect={"text": "Home"})]), "apply")
        assert "with 1 repair" in r["text"]
        fixed = json.loads((tmp_path / "sign_in.repaired.json").read_text("utf-8"))
        assert fixed["steps"][0]["target"] == {"id": "x-" + new.split()[0].lower()}
        await finish(stop, task)
    run(go)


DISMISS = ["dismiss-btn", "close-promo", "skip-tour", "not-now-btn", "later"]


@pytest.mark.parametrize("dismiss", DISMISS)
def test_a_new_screen_in_the_way_is_cleared_by_the_proven_road(tmp_path, dismiss):
    from test_daemon_routes import explore

    async def go():
        d = mapped(tmp_path)
        d.session.screens["promo"] = screen("Promo", ids=(dismiss, "p1", "p2"))
        d.session.goes.pop(("promo", "dismiss-btn"), None)
        d.session.goes[("promo", dismiss)] = "welcome"
        stop, task = await started(d)
        await explore(d)
        d.session.at = "promo"
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "map", {"name": "promo"})
        await d.run_tool("a", "tap", {"target": {"id": dismiss}})
        await d.maps.flush()
        d.session.at = "promo"
        await d.run_tool("a", "look", {"screenshot": False})
        steps = [tap({"id": "continue-btn"}, place="welcome", expect={"text": "Login"})]
        r = await replay(d, script(tmp_path, steps), "apply")
        assert f"tap id={dismiss} before it (promo to welcome)" in r["text"]
        fixed = json.loads((tmp_path / "sign_in.repaired.json").read_text("utf-8"))
        assert [s.get("target") for s in fixed["steps"]] == [{"id": dismiss}, {"id": "continue-btn"}]
        await finish(stop, task)
    run(go)


BUGS = [("save-btn", "Saved"), ("login-button", "Welcome back"), ("add-day-btn", "Day 1"), ("search-input", "3 results"),
        ("share-btn", "Link copied")]


@pytest.mark.parametrize("ident,text", BUGS)
def test_a_real_bug_is_reported_and_never_repaired(tmp_path, ident, text):
    async def go():
        tree = [title("Start"), button("e1", "Do it", ident, rect=(10, 600, 100, 40)),
                button("e2", "Do it too", ident + "-2", rect=(10, 500, 100, 40))]
        d = phone(tmp_path, Flow(tree, {ident: HOME}))  # the screen changes, but not to what the script expects
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, [tap({"id": ident}, expect={"text": text, "timeout": 0.5})]), "apply")
        assert e.value.code == "expect_failed" and "sign_in.repaired.json" not in [p.name for p in tmp_path.iterdir()]
        assert repair_lines(tmp_path) == []
        await finish(stop, task)
    run(go)


def test_the_set_as_a_whole_repairs_at_least_twelve_of_fifteen_and_no_wrong_one():
    assert len(RENAMES) + len(MOVED) + len(DISMISS) == 15 and len(BUGS) == 5  # each of them is asserted above, one by one
