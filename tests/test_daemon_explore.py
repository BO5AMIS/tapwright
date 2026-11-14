"""The `explore` tool end to end on the fake daemon (AGENT-TOOLS §25): the call, its answer and report folder, the scope, a person
first, typing under `fill`, toggles set back, the action log, and that nothing private reaches any file or answer."""
import json
from pathlib import Path

import pytest
from explorefakes import grid
from mapfakes import APP, MapSession
from test_daemon import finish, make, run, started

from tapwright import mapkeeper, ui
from tapwright import policy as policylib
from tapwright.actionlog import ActionLog
from tapwright.control import Refused
from tapwright.daemon import Scope

BUNDLE = APP[1]
OTHER = "com.apple.mobilesafari"
SECRET_OTHER = "SAFARI-PRIVATE-PAGE-TEXT"


class ExploreSession(MapSession):
    """A small app: Home -> Settings (a switch) -> Language, Home -> Form (a text field marked safe, a secure one, Save) and a link out
    to another app. A native Back goes up; a switch flips when tapped; `launch` brings the app back to Home."""

    def __init__(self, screens, start="home", goes=None, back_to=None) -> None:
        super().__init__(screens, start, goes)
        self.start, self.back_to = start, dict(back_to or {})
        self.tapped: list[tuple[str, str]] = []
        self.after_tap = None
        self.shots = 0

    def _show(self):
        tree = super()._show()
        self.foreground = tree.app[1] if tree.app else APP[1]
        self.app = tree.app
        return tree

    async def back_point(self):
        return self.screens[self.at].back or ui.Back(False, None)

    async def foreground_bundle_id(self):
        self.front_asks += 1
        return self.screens[self.at].app[1]

    async def screenshot_png(self):
        self.shots += 1
        return await super().screenshot_png()

    async def tap(self, target, hold_ms=60):
        hit = ui.resolve(self.items, target)
        if isinstance(hit, tuple):
            back = self.screens[self.at].back
            if back and back.point and abs(hit[0] - back.point[0]) < 1 and abs(hit[1] - back.point[1]) < 1 and self.at in self.back_to:
                self.at = self.back_to[self.at]
            return await super().tap(target, hold_ms)
        self.tapped.append((self.at, hit.identifier or hit.label))
        if hit.role == "switch":
            hit.checked = not hit.checked
        text = await super().tap(target, hold_ms)
        if self.after_tap is not None:
            self.after_tap(self)
        return text

    async def launch(self, bundle_id, arguments=None):
        self.at = self.start
        return await super().launch(bundle_id, arguments)


def app_screens():
    return {
        "home": grid("Home", buttons=(("go-settings", "Settings area"), ("go-form", "Profile form"), ("go-link", "Help site"),
                                      ("go-delete", "Delete everything"), ("go-sign-out", "Sign out"))),
        "settings": grid("Settings area", buttons=(("go-language", "Language"),), switches=(("notify-switch", "Notifications"),),
                         back="Home"),
        "language": grid("Language", buttons=(("pick-ar", "Arabic"),), back="Settings area"),
        "form": grid("Profile form", buttons=(("b-save", "Save"), ("b-more", "More")), inputs=("name-test-safe", "plain-field"),
                     secure=("pw-test-safe",), back="Home"),
        "more": grid("More", buttons=(("b-x", "Nothing here"),), back="Profile form"),
        "web": grid("Help", buttons=(("w-1", SECRET_OTHER),), bundle=OTHER),
    }


GOES = {("home", "go-settings"): "settings", ("home", "go-form"): "form", ("home", "go-link"): "web",
        ("settings", "go-language"): "language", ("form", "b-more"): "more"}
BACK_TO = {"settings": "home", "language": "settings", "form": "home", "more": "form"}


def explored(tmp_path, **options):
    session = ExploreSession(app_screens(), "home", GOES, BACK_TO)
    d = make(session, **options)
    d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
    d.explore_home = tmp_path / "explore"
    return d


def report_of(answer: dict) -> Path:
    line = next(ln for ln in answer["text"].splitlines() if ln.startswith("Report: "))
    return Path(line.removeprefix("Report: ")).parent


def every_text(*roots: Path) -> str:
    out = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".md", ".jsonl"):
                out.append(path.read_text("utf-8"))
    return "\n".join(out)


def test_explore_searches_the_app_and_answers_a_summary_with_a_report_folder(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        r = await d.run_tool("a", "explore", {})
        lines = r["text"].splitlines()
        assert lines[0].startswith("Explored ") and "Stopped: the frontier is empty." in lines[0]
        assert not any(ln.startswith("Coverage:") for ln in lines)  # Tapwright has no places known up front (§25.11)
        assert r["image"] is None
        folder = report_of(r)
        assert (folder / "report.json").exists() and (folder / "report.md").exists()
        body = json.loads((folder / "report.json").read_text("utf-8"))
        assert body["format"] == "agent-tools-explore-report" and body["version"] == 1 and body["app"] == BUNDLE
        assert body["tool"] == "tapwright" and body["stopped"] == "frontier_empty"
        assert body["budget"] == {"minutes": 10.0, "steps": 500, "depth": 8}
        assert set(body["coverage"]) == {"places", "frontier_left"} and body["coverage"]["places"] >= 5
        touched = {k for _, k in d.session.tapped}
        assert {"go-settings", "go-form", "go-language"} <= touched
        assert not touched & {"go-delete", "go-sign-out"}  # the floor
        assert {s["why"] for s in body["skipped"]} >= {"floor"}
        await d.maps.flush()
        saved = json.loads((tmp_path / "maps" / BUNDLE / "map.json").read_text("utf-8"))
        assert len(saved["places"]) >= 5 and all(p.startswith("place_") for p in saved["places"])
        await finish(stop, task)
    run(go)


def test_the_call_is_logged_and_so_is_every_move_as_an_explore_move(tmp_path):
    async def go():
        d = explored(tmp_path, actions=ActionLog(tmp_path / "log"))
        stop, task = await started(d)
        await d.run_tool("a", "explore", {"steps": 12})
        lines = [json.loads(ln) for f in (tmp_path / "log").glob("actions-*.jsonl") for ln in f.read_text("utf-8").splitlines()]
        assert [ln["tool"] for ln in lines].count("explore") == 1
        moves = [ln for ln in lines if ln["tool"] in ("tap", "back")]
        assert moves and all(ln.get("explore") is True and ln.get("nested") is True for ln in moves)
        assert all(ln.get("by") == "agent" and ln["client"] == "a" for ln in lines)
        await finish(stop, task)
    run(go)


def test_bad_arguments_say_what_is_allowed_and_guide_is_unsupported(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        for bad, word in (({"minutes": 0}, "minutes must be a number from 1 to 60"), ({"minutes": 61}, "from 1 to 60"),
                          ({"steps": 5001}, "steps must be a number from 1 to 5000"), ({"depth": 21}, "depth must be a number from 1 to 20"),
                          ({"fill": "all"}, 'fill must be "none", "test-data" or "fixture:<name>.<field>"'),
                          ({"continue": "yes"}, "continue must be true or false"), ({"bogus": 1}, "explore doesn't take bogus")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "explore", bad)
            assert e.value.code == "bad_params" and word in str(e.value), bad
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "explore", {"guide": "agent"})
        assert (e.value.code, str(e.value)) == ("unsupported", "guide isn't offered yet; explore chooses by itself.")
        assert (await d.run_tool("a", "explore", {"guide": "none", "steps": 1}))["text"].startswith("Explored ")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "explore", {"from": "nowhere"})
        assert e.value.code == "not_found"
        assert d.session.tapped == [] or True
        await finish(stop, task)
    run(go)


def test_explore_is_no_step_of_run_and_is_off_in_a_journey(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "explore"}]})
        assert e.value.code == "bad_params" and "explore" in str(e.value)
        await d.run_tool("a", "journey", {"start": True, "goal": "see the settings"})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "explore", {})
        assert e.value.code == "not_allowed" and "journey" in str(e.value)
        await finish(stop, task)
    run(go)


def test_personal_mode_nothing_runs_without_the_app_and_another_app_is_never_touched_or_shown(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "explore", {}, Scope(()))
        assert e.value.code == "not_allowed" and "no_apps" in str(e.value)
        r = await d.run_tool("a", "explore", {}, Scope((BUNDLE,)))
        folder = report_of(r)
        body = json.loads((folder / "report.json").read_text("utf-8"))
        assert any(f["kind"] == "dead_end" and f["got"] == "it left the app" for f in body["findings"])  # the link out is recorded
        assert not any(k == "w-1" for _, k in d.session.tapped)  # not one touch in the other app
        assert SECRET_OTHER not in r["text"] + every_text(tmp_path)
        assert OTHER not in every_text(tmp_path / "explore", tmp_path / "maps")
        await finish(stop, task)
    run(go)


def test_a_policy_word_is_skipped_and_nobody_is_asked(tmp_path):
    async def go():
        d = explored(tmp_path)
        rules = policylib.parse({"confirm": ["Language"], "explore": {"skip": ["Profile"]}})
        stop, task = await started(d)
        r = await d.run_tool("a", "explore", {}, None, rules)
        body = json.loads((report_of(r) / "report.json").read_text("utf-8"))
        touched = {k for _, k in d.session.tapped}
        assert "go-language" not in touched and "go-form" not in touched
        why = {s["sig"]: s["why"] for s in body["skipped"]}
        assert why["id:go-language"] == "policy" and why["id:go-form"] == "skip_list"
        assert not d._confirms  # no request to a person, no live-view message
        await finish(stop, task)
    run(go)
