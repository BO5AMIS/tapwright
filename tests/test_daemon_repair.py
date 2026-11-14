"""`replay` with `repair` through the daemon (AGENT-TOOLS §26.5, §26.8): suggest touches nothing, apply repairs as it goes and
accepts only what passes the one rule, a failure writes nothing, and what is never repaired never is."""
import json
import re

import pytest
from fakes import FakeSession, button, el
from mapfakes import APP, MapSession, screen
from test_daemon import finish, make, run, started

from tapwright import auditlog, mapkeeper, params, ui
from tapwright.control import Refused

BUNDLE = APP[1]


class Flow(FakeSession):
    """A phone whose tap on an element changes the screen: `goes[identifier or label] = the new screen's elements`."""

    def __init__(self, tree, goes=None):
        super().__init__()
        self.tree, self.goes = list(tree), dict(goes or {})

    async def tap(self, target, hold_ms=60):
        hit = ui.resolve(self.items, target)
        text = await super().tap(target, hold_ms)
        key = None if isinstance(hit, tuple) else hit.identifier or hit.label
        if key in self.goes:
            self.tree = list(self.goes[key])
        return text

    async def swipe(self, x0, y0, x1, y1, duration_ms=350, hold_ms=0):
        text = await super().swipe(x0, y0, x1, y1, duration_ms, hold_ms)
        if "scroll" in self.goes:  # a scroll makes what was below
            self.tree = list(self.goes["scroll"])
        return text


def title(text, ref="t1"):
    return el(ref, "text", text, rect=(10, 40, 200, 30))


HOME = [title("Home"), button("h1", "Go", "go", rect=(10, 600, 100, 40))]
WELCOME = [title("Welcome"), button("e1", "Log in", "login-button", rect=(10, 600, 100, 40)),
           button("e2", "Help", "help-button", rect=(10, 300, 100, 40))]


def phone(tmp_path, session, **options):
    d = make(session, audit=auditlog.AuditLog(tmp_path / "audit"), **options)
    d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
    return d


def script(tmp_path, steps, name="sign_in", **extra):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"format": "agent-tools-script", "version": 1, "name": name, "app": BUNDLE, "fresh": False,
                                "steps": steps, **extra}, indent=2), "utf-8")
    return path


def repair_lines(tmp_path):
    return [json.loads(line)["detail"] for line in auditlog.read_lines(tmp_path / "audit") if json.loads(line)["kind"] == "repair"]


def names(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if p.is_file())


def tap(target, **kw):
    return {"tool": "tap", "target": target, **kw}


async def replay(d, path, mode=None, **kw):
    args = {"path": str(path), **({"repair": mode} if mode else {}), **kw}
    return await d.run_tool("a", "replay", args)


RENAMED = [tap({"id": "login-btn"}, expect={"text": "Home"})]


# ---- the call ----

def test_repair_is_suggest_or_apply_and_never_beside_update_references(tmp_path):
    async def go():
        d = phone(tmp_path, Flow(WELCOME))
        stop, task = await started(d)
        path = script(tmp_path, RENAMED)
        for bad in ("fix", True, "adopt"):
            with pytest.raises(Refused) as e:
                await replay(d, path, bad)
            assert e.value.code == "bad_params" and 'repair must be "suggest" or "apply"' in str(e.value)
        with pytest.raises(Refused) as e:
            await replay(d, path, "apply", update_references=True)
        assert e.value.code == "bad_params" and "update_references" in str(e.value)
        assert "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_replay_has_no_parameter_that_adopts(tmp_path):
    assert "adopt" not in params.ALLOWED["replay"]
    assert "adopt" in params.refusal("replay", ["adopt"]) and params.unknown("replay", {"adopt": True}) == ["adopt"]

    async def go():
        d = phone(tmp_path, Flow(WELCOME))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, RENAMED), "apply", adopt=True)
        assert e.value.code == "bad_params" and "doesn't take adopt" in str(e.value)
        await finish(stop, task)
    run(go)


# ---- suggest ----

def test_suggest_lists_the_fix_after_the_failure_and_touches_nothing(tmp_path):
    async def go():
        d = phone(tmp_path, Flow(WELCOME, {"login-button": HOME}))
        stop, task = await started(d)
        path = script(tmp_path, RENAMED)
        before = names(tmp_path)
        with pytest.raises(Refused) as e:
            await replay(d, path, "suggest")
        text = str(e.value)
        assert e.value.code == "not_found" and text.startswith("Ran 0 of 1 steps; stopped at step 1 (tap id=login-btn): ")
        tail = text.split("\n")
        at = tail.index("Repair (suggest, not checked):")
        assert tail[at + 1].startswith('1. tap id=login-button, button "Log in": score 1.00 (id_stem 1.00)') and "Why:" in tail[at + 1]
        assert tail[-1] == 'To check it: replay with repair: "apply".'
        assert "tap" not in d.session.calls and names(tmp_path) == before and repair_lines(tmp_path) == []
        assert not (tmp_path / "maps").exists() or not any((tmp_path / "maps").rglob("map.json"))
        await finish(stop, task)
    run(go)


def test_suggest_says_ambiguous_when_the_best_two_cant_be_told_apart(tmp_path):
    async def go():
        tree = [title("Welcome"), button("e1", "Log in", "login-button", rect=(10, 600, 100, 40)),
                button("e2", "Log in", "login-buttons", rect=(10, 500, 100, 40))]
        d = phone(tmp_path, Flow(tree))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, RENAMED), "suggest")
        assert "Repair (suggest, not checked): ambiguous." in str(e.value) and "tap" not in d.session.calls
        await finish(stop, task)
    run(go)


def test_suggest_on_a_run_that_passes_is_a_plain_replay(tmp_path):
    async def go():
        d = phone(tmp_path, Flow(WELCOME, {"login-button": HOME}))
        stop, task = await started(d)
        r = await replay(d, script(tmp_path, [tap({"id": "login-button"}, expect={"text": "Home"})]), "suggest")
        assert r["text"].startswith("Ran 1 of 1 steps") and "Repair" not in r["text"]
        assert names(tmp_path)
        await finish(stop, task)
    run(go)


def test_suggest_says_why_nothing_is_suggested_for_a_step_never_repaired(tmp_path):
    async def go():
        d = phone(tmp_path, Flow(WELCOME))
        stop, task = await started(d)
        steps = [{"tool": "type_text", "target": {"id": "email"}, "text": "a@b.co"}]
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, steps), "suggest")
        assert "Repair (suggest): none. A type_text step is never repaired" in str(e.value)
        await finish(stop, task)
    run(go)


# ---- apply: what is repaired ----

def test_apply_repairs_a_renamed_id_checks_it_and_writes_the_files_beside_the_script(tmp_path):
    async def go():
        d = phone(tmp_path, Flow(WELCOME, {"login-button": HOME}))
        stop, task = await started(d)
        path = script(tmp_path, RENAMED + [tap({"id": "go"})])
        original = path.read_bytes()
        r = await replay(d, path, "apply")
        lines = r["text"].split("\n")
        assert re.fullmatch(r"Ran 2 of 2 steps in \d+\.\d s, with 1 repair \(not adopted\)\.", lines[0]), lines[0]
        assert lines[1] == "Repaired: step 1 tap id=login-btn to id=login-button (score 1.00)."
        assert lines[2] == "Wrote sign_in.repaired.json and sign_in.repair.md. The original is unchanged."
        assert path.read_bytes() == original  # never over the original
        fixed = json.loads((tmp_path / "sign_in.repaired.json").read_text("utf-8"))
        assert fixed["repaired"]["from"] == "sign_in.json" and fixed["repaired"]["tool"] == "tapwright" and fixed["repaired"]["count"] == 1
        assert fixed["repaired"]["build"] == "1.0 (7)" and fixed["name"] == "sign_in"  # the name stays: the references folder is the same
        step = fixed["steps"][0]
        assert step["target"] == {"id": "login-button"} and step["repaired"] == {"was": {"id": "login-btn"}, "score": 1.0}
        assert step["seen"]["role"] == "button" and "Log in" not in json.dumps(step["seen"])
        assert fixed["steps"][1] == {"tool": "tap", "target": {"id": "go"}}  # a step that held is as it was
        md = (tmp_path / "sign_in.repair.md").read_text("utf-8")
        assert 'Step 1 (tap). Was id=login-btn; now id=login-button (button "Log in").' in md
        assert "Accepted: the step's expect (text \"Home\") held, and the whole run passed." in md
        assert repair_lines(tmp_path) == ["step 1: tap id=login-btn to id=login-button (score 1.00), accepted"]
        # the repaired script replays green on its own
        d.session.tree = list(WELCOME)
        again = await replay(d, tmp_path / "sign_in.repaired.json")
        assert again["text"].startswith("Ran 2 of 2 steps")
        await finish(stop, task)
    run(go)


def test_the_next_steps_target_is_the_check_when_the_step_has_no_expect(tmp_path):
    async def go():
        d = phone(tmp_path, Flow(WELCOME, {"login-button": HOME}))
        stop, task = await started(d)
        r = await replay(d, script(tmp_path, [tap({"id": "login-btn"}), tap({"id": "go"})]), "apply")
        assert "with 1 repair" in r["text"]
        assert "the next step's target (id=go) resolved to one element" in (tmp_path / "sign_in.repair.md").read_text("utf-8")
        await finish(stop, task)
    run(go)


def test_a_last_step_with_no_expect_has_no_check_so_it_is_not_repaired(tmp_path):
    async def go():
        d = phone(tmp_path, Flow(WELCOME, {"login-button": HOME}))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, [tap({"id": "login-btn"})]), "apply")
        assert e.value.code == "not_found" and "no repair was made. The step has no check" in str(e.value)
        assert "tap" not in d.session.calls and "sign_in.repaired.json" not in names(tmp_path) and repair_lines(tmp_path) == []
        await finish(stop, task)
    run(go)


def test_a_moved_button_is_found_by_the_facts_the_recording_kept(tmp_path):
    async def go():
        # recorded when "Skip" was a button in the middle column's bottom row; now it says "Not now" and so does the Help one
        tree = [title("Welcome"), button("e1", "Not now", "later-btn", rect=(150, 700, 100, 40)),
                button("e2", "Help", "help-btn", rect=(10, 100, 100, 40))]
        d = phone(tmp_path, Flow(tree, {"later-btn": HOME}))
        stop, task = await started(d)
        seen = {"role": "button", "bucket": "c2r3", "component": "Button"}
        r = await replay(d, script(tmp_path, [tap({"text": "Skip"}, seen=seen, expect={"text": "Home"})]), "apply")
        assert "Repaired: step 1 tap \"Skip\" to id=later-btn" in r["text"]
        fixed = json.loads((tmp_path / "sign_in.repaired.json").read_text("utf-8"))
        assert fixed["steps"][0]["seen"]["bucket"] == "c2r3"  # a repaired step carries the facts of the element accepted
        await finish(stop, task)
    run(go)


def test_a_row_the_list_hadnt_made_yet_is_reached_by_scrolling(tmp_path):
    async def go():
        top = [title("Trips"), el("e1", "scroll", "", "trip-list", rect=(0, 100, 400, 500)),
               button("r1", "Trip 1", "trip-row-1", rect=(10, 150, 300, 40))]
        more = [*top, button("r9", "Trip 9", "trip-row-9", rect=(10, 400, 300, 40))]
        d = phone(tmp_path, Flow(top, {"scroll": more, "trip-row-9": HOME}))
        stop, task = await started(d)
        r = await replay(d, script(tmp_path, [tap({"id": "trip-row-9"}, expect={"text": "Home"})]), "apply")
        assert "with scroll down 1 time before it" in r["text"]
        fixed = json.loads((tmp_path / "sign_in.repaired.json").read_text("utf-8"))
        assert fixed["steps"][0] == {"tool": "scroll", "direction": "down", "repaired": {"inserted": True}}
        assert fixed["steps"][1]["target"] == {"id": "trip-row-9"} and "repaired" not in fixed["steps"][1]
        assert repair_lines(tmp_path)[0].startswith("step 1: tap id=trip-row-9 with scroll down 1 time before it, accepted")
        await finish(stop, task)
    run(go)


# ---- apply: what is refused ----

def test_a_wrong_element_that_fails_its_own_check_is_rejected_and_nothing_is_written(tmp_path):
    async def go():
        # login-button looks like login-btn but does not lead to Home: the real one is "sign-in"
        tree = [title("Welcome"), button("e1", "Log in", "login-button", rect=(10, 600, 100, 40))]
        d = phone(tmp_path, Flow(tree))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, RENAMED), "apply")
        assert e.value.code == "not_found" and "the candidate was tried and none passed the check" in str(e.value)
        assert d.session.calls.count("tap") == 1  # the trial touched the app: apply is a real run
        assert "sign_in.repaired.json" not in names(tmp_path) and "sign_in.repair.md" not in names(tmp_path)
        [line] = repair_lines(tmp_path)
        assert line.startswith("step 1: tap id=login-btn to id=login-button (score 1.00), rejected (")
        await finish(stop, task)
    run(go)


def test_a_wrong_element_that_passes_its_check_but_breaks_the_run_is_not_kept(tmp_path):
    async def go():
        wrong = [title("Home"), button("h1", "Else", "elsewhere", rect=(10, 600, 100, 40))]  # shows Home, has no `go`
        d = phone(tmp_path, Flow(WELCOME, {"login-button": wrong}))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, RENAMED + [tap({"id": "go"}, expect={"text": "Done"})]), "apply")
        text = str(e.value)
        assert e.value.code == "not_found" and "Ran 1 of 2 steps; stopped at step 2 (tap id=go)" in text
        assert "Note: 1 repair was made before it stopped; nothing was written." in text
        assert names(tmp_path) == ["sign_in.json"]
        assert repair_lines(tmp_path) == ["step 1: tap id=login-btn to id=login-button (score 1.00), accepted",
                                          "step 1: tap id=login-btn to id=login-button (score 1.00), rejected (the run failed at step 2)"]
        await finish(stop, task)
    run(go)


def test_a_real_bug_an_expect_that_fails_is_reported_and_never_repaired(tmp_path):
    async def go():
        d = phone(tmp_path, Flow(WELCOME, {"login-button": HOME}))
        stop, task = await started(d)
        steps = [tap({"id": "login-button"}, expect={"text": "Welcome back", "timeout": 0.5})]
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, steps), "apply")
        assert e.value.code == "expect_failed" and "no repair was made. An expect that fails is a finding" in str(e.value)
        assert names(tmp_path) == ["sign_in.json"] and repair_lines(tmp_path) == []
        assert d.session.calls.count("tap") == 1
        await finish(stop, task)
    run(go)


def test_a_failure_that_cant_be_repaired_writes_no_file_and_says_what_was_made_before_it(tmp_path):
    async def go():
        d = phone(tmp_path, Flow(WELCOME, {"login-button": HOME}))
        stop, task = await started(d)
        steps = [*RENAMED, {"tool": "type_text", "target": {"id": "email"}, "text": "x"}]
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, steps), "apply")
        assert "Note: 1 repair was made before it stopped; nothing was written." in str(e.value)
        assert "sign_in.repaired.json" not in names(tmp_path) and "sign_in.repair.md" not in names(tmp_path)
        await finish(stop, task)
    run(go)


def test_a_policy_word_a_typing_step_a_native_back_and_other_tools_are_never_repaired(tmp_path):
    async def go():
        tree = [title("Welcome"), button("e1", "Delete trip now", "delete-trip-btn", rect=(10, 600, 100, 40)),
                button("e2", "Log in", "login-button", rect=(10, 500, 100, 40)), button("e3", "Trips", "BackButton", rect=(10, 50, 60, 30))]
        d = phone(tmp_path, Flow(tree))
        stop, task = await started(d)
        for step, why in ((tap({"text": "Delete trip"}, expect={"text": "x"}), "carries a policy word"),
                          (tap({"id": "delete-row-btn"}, expect={"text": "x"}), "carries a policy word"),
                          ({"tool": "type_text", "target": {"id": "login-btn"}, "text": "x", "expect": {"text": "x"}}, "type_text step"),
                          (tap({"label": "Back"}, expect={"text": "x"}), "native Back"),
                          (tap({"id": "nav-back-button"}, expect={"text": "x"}), "native Back"),
                          ({"tool": "swipe", "x0": 1, "y0": 1, "x1": 2, "y1": 2}, None)):
            if why is None:  # a swipe that works is no break at all
                continue
            with pytest.raises(Refused) as e:
                await replay(d, script(tmp_path, [step]), "apply")
            assert why in str(e.value), (step, str(e.value))
        assert "tap" not in d.session.calls and repair_lines(tmp_path) == []
        await finish(stop, task)
    run(go)


def test_a_step_in_a_draftsonly_route_is_never_repaired(tmp_path):
    from test_daemon_routes import GOES, explore, folder, screens
    async def go():
        d = phone(tmp_path, MapSession(screens(), "welcome", GOES))
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        await explore(d)
        d.session.screens["welcome"] = screen("Welcome", ids=("continue-button", "w1", "w2"))
        d.session.at = "welcome"
        await d.run_tool("a", "look", {"screenshot": False})
        route = {"format": "agent-tools-script", "version": 2, "name": "sign_in", "app": BUNDLE, "fresh": False, "start": "welcome",
                 "end": "login", "variables": [], "draftsOnly": True,
                 "steps": [tap({"id": "continue-btn"}, expect={"text": "Login"})]}
        path = tmp_path / "sign_in.json"
        path.write_text(json.dumps(route), "utf-8")
        with pytest.raises(Refused) as e:
            await replay(d, path, "apply")
        assert "draftsOnly is never repaired" in str(e.value)
        assert "sign_in.repaired.json" not in names(tmp_path) and repair_lines(tmp_path) == []
        route["draftsOnly"] = False
        path.write_text(json.dumps(route), "utf-8")
        d.session.goes[("welcome", "continue-button")] = "login"
        r = await replay(d, path, "apply")  # the same break in a route that isn't drafts-only is repaired
        assert "with 1 repair" in r["text"]
        await finish(stop, task)
    run(go)


# ---- the limits (§26.4) ----

def test_at_most_three_candidates_are_tried_for_a_step(tmp_path):
    async def go():
        tree = [title("Home"), *(button(f"e{n}", f"B{n}", ident, rect=(10, 100 + 60 * n, 100, 40))
                                 for n, ident in enumerate(("save-btn2", "save-x", "save", "my-save-it"), 1))]
        d = phone(tmp_path, Flow(tree))
        stop, task = await started(d)
        steps = [tap({"id": "save-btn"}, expect={"text": "Saved", "timeout": 0.5})]
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, steps), "apply")
        assert "the first 3 candidates were tried and none passed the check" in str(e.value)
        assert d.session.calls.count("tap") == 3  # the fourth is never tried
        assert len([line for line in repair_lines(tmp_path) if "rejected" in line]) == 3
        await finish(stop, task)
    run(go)


def test_at_most_five_repairs_are_made_in_one_script(tmp_path):
    async def go():
        tree = [title("Home"), *(button(f"e{n}", f"B{n}", f"s{n}-button", rect=(10, 60 + 50 * n, 100, 40)) for n in range(1, 7))]
        d = phone(tmp_path, Flow(tree))
        stop, task = await started(d)
        steps = [tap({"id": f"s{n}-btn"}, expect={"id": f"s{n}-button"}) for n in range(1, 7)]
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, steps), "apply")
        text = str(e.value)
        assert "Ran 5 of 6 steps; stopped at step 6" in text and "Note: 5 repairs were made and the script still breaks" in text
        assert "Note: 5 repairs were made before it stopped; nothing was written." in text
        assert "sign_in.repaired.json" not in names(tmp_path)
        await finish(stop, task)
    run(go)


def test_a_repair_needs_its_audit_line(tmp_path):
    async def go():
        d = make(Flow(WELCOME, {"login-button": HOME}))  # no audit log at all
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, RENAMED), "apply")
        assert "the audit log couldn't be written, so no repair was made" in str(e.value) and "tap" not in d.session.calls
        await finish(stop, task)
        # a log that can't be written to: the trial is made, its line can't be, so the repair isn't
        blocked = tmp_path / "blocked"
        blocked.write_text("a file where the log's folder should be", "utf-8")
        d = make(Flow(WELCOME, {"login-button": HOME}), audit=auditlog.AuditLog(blocked))
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await replay(d, script(tmp_path, RENAMED), "apply")
        assert "audit log couldn't be written" in str(e.value) and "sign_in.repaired.json" not in names(tmp_path)
        await finish(stop, task)
    run(go)


# ---- a new screen in the way, and what the map learns ----

def mapped(tmp_path, goes=None):
    from test_daemon_routes import GOES, screens
    all_screens = {**screens(), "promo": screen("Promo", ids=("dismiss-btn", "p1", "p2"))}
    d = phone(tmp_path, MapSession(all_screens, "welcome", {**GOES, ("promo", "dismiss-btn"): "welcome", **(goes or {})}))
    return d


def test_a_new_screen_in_the_way_is_cleared_by_a_proven_road_put_before_the_step(tmp_path):
    from test_daemon_routes import explore
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        d.session.at = "promo"
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "map", {"name": "promo"})
        await d.run_tool("a", "tap", {"target": {"id": "dismiss-btn"}})  # promo > welcome is a proven road now
        await d.maps.flush()
        d.session.at = "promo"
        steps = [tap({"id": "continue-btn"}, place="welcome", expect={"text": "Login"})]
        path = script(tmp_path, steps)
        with pytest.raises(Refused):
            await replay(d, path)  # the plain replay stops: the promo is in the way
        d.session.at = "promo"
        r = await replay(d, path, "apply")
        assert "with tap id=dismiss-btn before it (promo to welcome)" in r["text"]
        fixed = json.loads((tmp_path / "sign_in.repaired.json").read_text("utf-8"))
        assert fixed["steps"][0] == {"tool": "tap", "target": {"id": "dismiss-btn"}, "repaired": {"inserted": True}}
        assert fixed["steps"][1]["place"] == "welcome" and "repaired" not in fixed["steps"][1]
        await finish(stop, task)
    run(go)


def test_apply_that_passes_teaches_the_map_the_road_that_moved_and_nothing_else(tmp_path):
    from test_daemon_routes import explore
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await d.maps.flush()
        d.session.screens["welcome"] = screen("Welcome", ids=("continue-button", "w1", "w2"))
        d.session.goes[("welcome", "continue-button")] = "login"
        d.session.at = "welcome"
        await d.run_tool("a", "look", {"screenshot": False})  # (the screen changed under the daemon's last read)
        steps = [tap({"id": "continue-btn"}, place="welcome", expect={"text": "Login"})]
        path = script(tmp_path, steps)
        root = tmp_path / "maps"
        raw = lambda: json.loads(next(root.rglob("map.json")).read_text("utf-8"))  # noqa: E731
        await d.maps.flush()
        data = raw()
        before_roads = [r for r in data["roads"] if r["from"] == "welcome"]
        assert before_roads and before_roads[0]["move"]["target"] == {"id": "continue-btn"}
        # a suggest and a failed apply leave the map as it was
        with pytest.raises(Refused):
            await replay(d, path, "suggest")
        await d.maps.flush()
        assert raw() == data
        await replay(d, path, "apply")
        await d.maps.flush()
        after = [r for r in raw()["roads"] if r["from"] == "welcome"]
        assert after[0]["move"]["target"] == {"id": "continue-button"} and after[0]["to"] == "login"
        assert after[0]["names"].get("id") == "continue-button" and after[0]["build"] == "1.0 (7)"
        await finish(stop, task)
    run(go)


# ---- recording keeps what a repair needs ----

def test_a_recording_saves_the_place_a_step_ran_on_and_the_facts_of_its_element(tmp_path):
    from test_daemon_routes import explore
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        await d.run_tool("a", "record", {"start": True, "name": "r"})
        await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
        path = tmp_path / "r.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        [step] = json.loads(path.read_text("utf-8"))["steps"]
        assert step["place"] == "welcome" and step["target"] == {"id": "continue-btn"}
        assert step["seen"]["role"] == "button" and step["seen"]["id_stem"] == "continue" and re.fullmatch(r"c\dr\d", step["seen"]["bucket"])
        assert "Label of" not in json.dumps(step["seen"])  # facts only: no label, no text
        # a replay never passes either note to the step's call
        d.session.at = "welcome"
        r = await d.run_tool("a", "replay", {"path": str(path)})
        assert r["text"].startswith("Ran 1 of 1 steps")
        await finish(stop, task)
    run(go)


# ---- privacy (§26.6, §20.3) and scope ----

def test_nothing_private_goes_into_the_files_or_the_audit_log(tmp_path):
    async def go():
        tree = [title("Welcome"), button("e1", "Log in as 12 Kestrel Lane", "login-button", rect=(10, 600, 100, 40))]
        d = phone(tmp_path, Flow(tree, {"login-button": HOME}))
        d.scrub_everywhere = lambda text: text.replace("12 Kestrel Lane", "<home's street>")
        d.audit.scrub = d.scrub_everywhere
        stop, task = await started(d)
        steps = [tap({"id": "login-btn"}, expect={"text": "Home"})]
        r = await replay(d, script(tmp_path, steps), "apply")
        assert "with 1 repair" in r["text"]
        for name in ("sign_in.repaired.json", "sign_in.repair.md"):
            assert "Kestrel" not in (tmp_path / name).read_text("utf-8"), name
        assert "<home's street>" in (tmp_path / "sign_in.repair.md").read_text("utf-8")
        assert all("Kestrel" not in line for line in auditlog.read_lines(tmp_path / "audit"))
        await finish(stop, task)
    run(go)


def test_a_repair_reads_nothing_of_another_app(tmp_path):
    async def go():
        d = phone(tmp_path, Flow(WELCOME))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.app = ("Other", "com.other.app")
        got = await d._repair_screen("a", None, BUNDLE, tap({"id": "login-btn"}))
        assert got is None  # the app in front isn't the script's: its screen is not read into a repair
        await finish(stop, task)
    run(go)
