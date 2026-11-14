"""PR #10, title scope: a navigation title is kept out of AUTOMATICALLY LEARNED map data only (AGENT-TOOLS §18.1): a place
is `place_<n>` until a person names it, and a learned road keeps no title in plain text (a native Back tap is learned as
`back`). What a user or an agent records or defines keeps what was recorded or given, titles included: a script, a route,
their waits, expectations and ignore entries, and a reflex (Codex, PR #10 cycle 6, findings 2, 4, 5 and 7)."""
from fakes import bare
import json
from xml.etree import ElementTree as ET

import pytest
from mapfakes import APP, SCREEN, MapSession
from test_back_in_recordings import view
from test_daemon import finish, make, run, started
from test_daemon_accounts import BUNDLE
from test_diagnostics import tuned

from tapwright import mapkeeper, ui
from test_account_values_raw_inside import node

SARA = "Chat with Sara"


def _xml(title: str, buttons) -> str:
    app = node("Application", bundleId=BUNDLE, label=APP[0], x=0, y=0, width=400, height=800)
    bar = node("NavigationBar", name=title, x=0, y=40, width=400, height=60)
    bar.append(node("StaticText", label=title, name=title, value=title, x=120, y=50, width=200, height=30,
                    traits="StaticText,Header"))
    app.append(bar)
    for b in buttons:
        app.append(b)
    return ET.tostring(app, encoding="unicode")


def home_chat():
    """Home has a button labelled with the chat's title (no id); the chat screen is titled with it."""
    home = ui.read(_xml("Home", [node("Button", label=SARA, name="", x=10, y=300, width=200, height=40)]), SCREEN)
    chat = ui.read(_xml(SARA, [node("Button", label="Reply", name="reply", x=10, y=300, width=200, height=40)]), SCREEN)
    return MapSession({"home": home, "chat": chat}, "home", {("home", SARA): "chat"})


def everything(folder) -> str:
    return "\n".join(p.read_text("utf-8", errors="replace") for p in folder.rglob("*") if p.is_file())


def map_json(tmp_path) -> dict:
    return json.loads((tmp_path / "maps" / BUNDLE / "map.json").read_text("utf-8"))


# ---- findings 2, 4: a recording keeps the title as recorded and replays to the same screen; map.json never holds it ----
def test_a_script_keeps_chat_with_sara_and_replays_to_the_same_screen(tmp_path):
    async def go():
        d = make(home_chat())
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "sara"})
        await d.run_tool("a", "tap", {"target": {"label": SARA}, "look": False})
        await d.run_tool("a", "wait_for", {"text": SARA, "look": False})  # the synchronisation a replay needs
        script = tmp_path / "sara.json"
        said = (await d.run_tool("a", "record", {"stop": True, "path": str(script)}))["text"]
        assert said.startswith("Saved 1 steps"), said
        assert bare(json.loads(script.read_text("utf-8"))["steps"]) == [
            {"tool": "tap", "target": {"label": SARA}, "expect": {"text": SARA}}]
        d.session.at = "home"
        said = (await d.run_tool("a", "replay", {"path": str(script)}))["text"]
        assert said.startswith("Ran 1 of 1 steps"), said
        assert d.session.at == "chat"
        await d.maps.flush()
        data = map_json(tmp_path)
        assert "Sara" not in json.dumps(data)  # place names and learned roads: no title
        assert all(name.startswith("place_") for name in data["places"])
        await finish(stop, task)
    run(go)


def test_a_route_keeps_the_title_it_was_recorded_with_and_the_map_holds_none(tmp_path):
    async def go():
        d = make(home_chat())
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "route": "to_sara"})
        await d.run_tool("a", "tap", {"target": {"label": SARA}, "look": False})
        await d.run_tool("a", "wait_for", {"label": SARA, "look": False})
        await d.run_tool("a", "record", {"stop": True})
        await d.maps.flush()
        folder = tmp_path / "maps" / BUNDLE
        route = json.loads(next((folder / "routes").glob("to_sara*.json")).read_text("utf-8"))
        assert bare(route["steps"]) == [{"tool": "tap", "target": {"label": SARA}, "expect": {"label": SARA}}]
        data = map_json(tmp_path)
        assert "Sara" not in json.dumps(data)
        assert all(name.startswith("place_") for name in data["places"])
        await finish(stop, task)
    run(go)


def test_a_title_wait_after_a_performance_step_is_kept_as_a_step(tmp_path):
    from tapwright.performance import Measurement

    async def go():
        d = tuned(make(home_chat()))
        d.session.device.measurement = Measurement(memory_mb=[100.0])
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "perf"})
        await d.run_tool("a", "performance", {"tap": {"label": SARA}, "seconds": 0.5})
        await d.run_tool("a", "wait_for", {"text": SARA, "look": False})
        script = tmp_path / "perf.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        steps = json.loads(script.read_text("utf-8"))["steps"]
        assert [s["tool"] for s in steps] == ["performance", "wait_for"]
        assert steps[0]["tap"] == {"label": SARA} and steps[1]["text"] == SARA
        await finish(stop, task)
    run(go)


# ---- finding 5: an ignore entry is kept as given ----
def test_an_ignore_selected_by_a_title_is_kept_as_given(tmp_path):
    async def go():
        d = make(home_chat())
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "ign"})
        await d.run_tool("a", "compare_screen", {"name": "home", "ignore": [{"label": SARA}]})
        await d.run_tool("a", "tap", {"target": {"label": SARA}, "look": False})  # (SARA is a place's title from here)
        script = tmp_path / "ign.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        steps = json.loads(script.read_text("utf-8"))["steps"]
        assert steps[0] == {"tool": "compare_screen", "name": "home", "ignore": [{"label": SARA}]}
        assert bare(steps[1]) == {"tool": "tap", "target": {"label": SARA}}
        await finish(stop, task)
    run(go)


# ---- finding 7: a reflex the agent defines keeps its condition ----
def test_a_reflex_keeps_its_when_text(tmp_path):
    async def go():
        d = make(home_chat())
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        await d.run_tool("a", "tap", {"target": {"label": SARA}, "look": False})
        await d.run_tool("a", "look", {"screenshot": False})
        reflex = {"name": "reply_prompt", "when": {"text": SARA}, "do": {"tool": "tap", "target": {"id": "reply"}}}
        said = (await d.run_tool("a", "map", {"reflex": reflex}))["text"]
        assert said == "Reflex added: reply_prompt.", said
        await d.maps.flush()
        data = map_json(tmp_path)
        assert data["reflexes"][0]["when"] == {"text": SARA}
        assert "Sara" not in json.dumps({k: v for k, v in data.items() if k != "reflexes"})  # (only where it was given)
        await finish(stop, task)
    run(go)


# ---- learned map data: a native Back is learned as `back`, with no name ----
def native_back():
    prev = view(SARA, buttons=("open",))
    detail = view("Detail", leading=(SARA, "BackButton"))
    return MapSession({"prev": prev, "detail": detail}, "prev",
                      {("prev", "open"): "detail", ("detail", "BackButton"): "prev"})


def test_a_recorded_native_back_tap_keeps_its_label_while_the_learned_road_is_back(tmp_path):
    async def go():
        d = make(native_back())
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"id": "open"}})
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "back"})
        await d.run_tool("a", "tap", {"target": {"label": SARA}})
        script = tmp_path / "back.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        assert bare(json.loads(script.read_text("utf-8"))["steps"]) == [{"tool": "tap", "target": {"label": SARA}}]
        await d.maps.flush()
        data = map_json(tmp_path)
        assert "Sara" not in json.dumps(data)
        backs = [r for r in data["roads"] if r["move"] == {"tool": "back"}]
        assert backs and all(r["names"] == {} for r in backs)
        await finish(stop, task)
    run(go)


def test_a_long_press_on_a_native_back_is_not_learned_as_a_back_road(tmp_path):
    d = make(native_back())
    d.maps = mapkeeper.Maps(str(tmp_path))

    async def go():
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"id": "open"}})
        await d.run_tool("a", "long_press", {"target": {"label": SARA}})
        await d.maps.flush()
        await finish(stop, task)
    run(go)
    text = (tmp_path / BUNDLE / "map.json").read_text("utf-8")
    moves = [r["move"] for r in json.loads(text)["roads"]]
    assert {"tool": "back"} not in moves and "Sara" not in text
    assert moves and all(m.get("target", {}).get("id") in ("open", "BackButton") for m in moves)


@pytest.mark.parametrize("how", ["script", "route"])
def test_nothing_in_a_recording_is_rewritten_for_a_title_learned_later(tmp_path, how):
    """A label that turns out to be the destination's title stays the label: replay finds the button by it."""
    async def go():
        d = make(home_chat())
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        if how == "script":
            await d.run_tool("a", "record", {"start": True, "name": "s"})
        else:
            await d.run_tool("a", "record", {"start": True, "route": "r"})
        await d.run_tool("a", "tap", {"target": {"label": SARA}, "look": False})
        await d.run_tool("a", "tap", {"target": {"label": "Reply"}, "look": False})
        if how == "script":
            script = tmp_path / "s.json"
            await d.run_tool("a", "record", {"stop": True, "path": str(script)})
            steps = json.loads(script.read_text("utf-8"))["steps"]
        else:
            await d.run_tool("a", "record", {"stop": True})
            await d.maps.flush()
            steps = json.loads(next((tmp_path / "maps" / BUNDLE / "routes").glob("r*.json")).read_text("utf-8"))["steps"]
        assert bare(steps) == [{"tool": "tap", "target": {"label": SARA}}, {"tool": "tap", "target": {"label": "Reply"}}]
        await finish(stop, task)
    run(go)
