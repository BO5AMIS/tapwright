"""Account values: raw inside, redacted at every edge (AGENT-TOOLS §18.6; Codex, PR #10 cycle 3).

The tree Tapwright reads keeps the screen's own words, so what it decides with them works as it does without accounts:
the safety policy (confirm and block words, an alert's buttons too), matching a target or an expectation, erasing a
field before typing. What leaves the process (an answer, an error, the live view, a file) shows a test account's value
as its placeholder, and a recording's file keeps its structure (a tool name that is also a value stays the tool)."""
import asyncio
import json
from xml.etree import ElementTree as ET

import pytest
from mapfakes import APP, SCREEN, MapSession
from test_account_values_at_ingest import assert_absent
from test_daemon import finish, make, run, started
from test_daemon_accounts import BUNDLE, write_accounts
from test_session import Recorder

from tapwright import accounts as accountlib
from tapwright import batch, policy, routes, ui
from tapwright import session as session_module
from tapwright.control import Refused
from tapwright.session import Session

USERNAME = "tester2@example.com"
LONG_USER = "very-long-test-account-name@example.com"  # Codex's erase trace: longer than its placeholder


def node(kind, **attrs):
    return ET.Element(f"XCUIElementType{kind}", {"type": f"XCUIElementType{kind}", "enabled": "true",
                                                 **{k: str(v) for k, v in attrs.items()}})


class XmlSession(MapSession):
    """One screen, read from XML inside each call (as WebDriverAgent's /source is): `buttons` are (id, label), `texts`
    static texts, `field` an input's value, `alert` (title, buttons)."""

    def __init__(self) -> None:
        super().__init__({}, "only")
        self.buttons: list[tuple[str, str]] = []
        self.texts: list[str] = []
        self.field: str | None = None
        self.headings: list[tuple[str, str]] = []  # (id, label) static texts
        self.switches: list[tuple[str, str]] = []  # (id, label), off
        self.alert_shown: tuple[str, tuple[str, ...]] | None = None

    def _show(self):
        app = node("Application", bundleId=BUNDLE, label=APP[0], x=0, y=0, width=400, height=800)
        bar = node("NavigationBar", name="Profile", x=0, y=40, width=400, height=60)
        bar.append(node("StaticText", label="Profile", name="Profile", value="Profile", x=10, y=50, width=200, height=30,
                        traits="StaticText,Header"))
        app.append(bar)
        for n, (ident, label) in enumerate(self.buttons):
            app.append(node("Button", label=label, name=ident, x=10, y=200 + 44 * n, width=300, height=40))
        for n, text in enumerate(self.texts):
            app.append(node("StaticText", label=text, name=text, value=text, x=10, y=400 + 24 * n, width=300, height=20,
                            traits="StaticText"))
        for n, (ident, label) in enumerate(self.headings):
            app.append(node("StaticText", label=label, name=ident, value=label, x=10, y=100 + 24 * n, width=300, height=20,
                            traits="StaticText"))
        for n, (ident, label) in enumerate(self.switches):
            app.append(node("Switch", label=label, name=ident, value="0", x=300, y=130 + 44 * n, width=51, height=31))
        if self.field is not None:
            app.append(node("TextField", label="Email", name="email", value=self.field, x=10, y=520, width=300, height=40))
        if self.alert_shown is not None:
            title, buttons = self.alert_shown
            box = node("Alert", label=title, x=50, y=250, width=300, height=200)
            for n, b in enumerate(buttons):
                box.append(node("Button", label=b, name=b, x=60 + 140 * n, y=400, width=130, height=40))
            app.append(box)
        tree = ui.read(ET.tostring(app, encoding="unicode"), SCREEN)  # (inside the call: as the daemon reads)
        self.items, self.last_tree = list(tree.items), tree
        self.last_insets = (tree.top_inset, tree.bottom_inset)
        self.last_alert, self.last_keyboard = tree.alert, tree.keyboard
        return tree


def daemon(tmp_path, username=USERNAME, password="pw-secret-0042", name="tester2"):
    session = XmlSession()
    d = make(session)
    data = {"version": 1, "accounts": {name: {"username": username, "password": password, "apps": [BUNDLE]}}}
    d.ceiling = type("C", (), {"accounts": write_accounts(tmp_path, data, "raw.json"), "brief": False, "maps": None})()
    sent: list[dict] = []
    d.broadcast = sent.append  # (what the live view is sent)
    d.sent = sent
    return d


# ---- P1: the policy decides on the screen's own words ----
@pytest.mark.parametrize("rule", ["confirm", "block"])
def test_a_password_that_is_a_policy_word_still_holds_the_button_back(tmp_path, rule):
    """Codex's trace: password `Delete`, a button id=remove-record labelled "Delete". The default confirmation (and a
    block rule) still holds the tap back; what is said and sent shows the placeholder."""
    async def go():
        d = daemon(tmp_path, password="Delete")
        d.session.buttons = [("remove-record", "Delete"), ("other", "Keep")]
        stop, task = await started(d)
        rules = policy.DEFAULT if rule == "confirm" else policy.Policy(block=("Delete",))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "remove-record"}, "look": False}, policy=rules)
        said = str(e.value)
        assert e.value.code == "not_allowed"
        assert said.startswith("needs_confirmation" if rule == "confirm" else "blocked"), said
        assert "tap" not in d.session.calls, "nothing was touched"
        assert "<tester2's password>" in said and "Delete" not in said
        assert "Delete" not in json.dumps(d.sent), d.sent  # the live view's request: caption and word
        await finish(stop, task)
    run(go)


def test_a_password_that_is_a_policy_word_still_holds_an_alerts_accept_back(tmp_path):
    async def go():
        d = daemon(tmp_path, password="Delete")
        d.session.alert_shown = ("Remove this record?", ("Cancel", "Delete"))
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "alert", {"action": "accept", "look": False})
        assert str(e.value).startswith("needs_confirmation"), str(e.value)
        assert "alert" not in d.session.calls
        assert "Delete" not in str(e.value) and "Delete" not in json.dumps(d.sent)
        await finish(stop, task)
    run(go)


# ---- P2: erasing counts the field's real text, never the placeholder ----
@pytest.mark.parametrize("submit", [True, False])
def test_replacing_a_field_that_holds_a_long_username_erases_all_of_it(monkeypatch, submit):
    """Codex's trace: the field holds a 39-character username (its placeholder is 14). Typing `new` into the still
    focused field with replace (and submit) leaves `new`, not the username's head and `new`. The read between the two
    is the answer's (look) or, with look:false, the next call's own resolve."""
    monkeypatch.setattr(session_module.store, "load_state", lambda: {"runner_id": "runner"})
    monkeypatch.setattr(session_module, "FOCUS_POLL", 0.01)
    phone = Recorder()
    field = {"value": ""}
    original = phone.http

    async def http(method, path, payload=None, timeout=30.0):
        if path == ui.SOURCE_PATH:
            app = node("Application", bundleId=BUNDLE, label=APP[0], x=0, y=0, width=430, height=932)
            app.append(node("TextField", label="Email", name="email", value=field["value"], x=10, y=200, width=300, height=40))
            return {"value": "<AppiumAUT>" + ET.tostring(app, encoding="unicode") + "</AppiumAUT>"}
        if path.endswith("/element/active"):
            return {"value": {"ELEMENT": "f0"}}
        if path.endswith("/element/f0/attribute/name"):
            return {"value": "email"}
        if path.endswith("/element/f0/attribute/value"):
            return {"value": field["value"]}
        if path.endswith("/element/f0/rect"):
            return {"value": {"x": 10, "y": 200, "width": 300, "height": 40}}
        if path.endswith("/element/f0/clear"):
            field["value"] = ""
        if path.endswith("/wda/keys"):
            for key in payload["value"]:
                field["value"] = field["value"][:-1] if key == "\b" else field["value"] + ("" if key == "\n" else key)
        return await original(method, path, payload, timeout)
    phone.http = http
    loaded = accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", LONG_USER, None)})
    token = ui.REDACT.set(loaded.scrub)  # (as in an agent's call)
    try:
        async def go():
            s = Session(phone)
            await s.refresh()
            await s.type_text(LONG_USER, replace=True, target=ui.Target(id="email"))
            assert field["value"] == LONG_USER
            await s.refresh()
            await s.type_text("new", replace=True, submit=submit, target=ui.Target(id="email"))
            return s
        s = asyncio.run(go())
    finally:
        ui.REDACT.reset(token)
    assert field["value"] == "new", field["value"]
    assert s.typed_into is None if submit else s.typed_into is not None


# ---- P2: a selector matches the screen's words or the placeholder they are shown as ----
def test_an_old_script_with_the_raw_value_and_a_selector_with_the_placeholder_both_find_the_button(tmp_path):
    async def go():
        d = daemon(tmp_path)
        d.session.buttons = [("profile-btn", USERNAME), ("l1", "Other")]
        d.session.texts = [f"Signed in as {USERNAME}"]
        d.session.field = USERNAME
        stop, task = await started(d)
        looked = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        assert "<tester2's username>" in looked and USERNAME not in looked
        path = tmp_path / "old.json"  # a version 1 script written before: the raw value as its selectors
        path.write_text(json.dumps({**batch.script("old", BUNDLE, [
            {"tool": "tap", "target": {"label": USERNAME}},
            {"tool": "tap", "target": {"text": USERNAME, "index": 0}},  # (the field shows it too)
            {"tool": "tap", "target": {"id": "l1"}, "expect": {"text": USERNAME}},
            {"tool": "wait_for", "label": USERNAME}]), "fresh": False}), "utf-8")
        said = (await d.run_tool("a", "replay", {"path": str(path)}))["text"]
        assert said.startswith("Ran 4 of 4 steps"), said
        said = (await d.run_tool("a", "run", {"steps": [
            {"tool": "tap", "target": {"label": "<tester2's username>"}},
            {"tool": "tap", "target": {"text": "<tester2's username>", "index": 1}},  # (the field)
            {"tool": "tap", "target": {"text": "Signed in as <tester2's username>"}},
            {"tool": "tap", "target": {"id": "l1"}, "expect": {"text": "<tester2's username>"}},
            {"tool": "tap", "target": {"id": "l1"}, "expect": {"id": "email", "value": "<tester2's username>"}},
            {"tool": "tap", "target": {"id": "l1"}, "expect": {"id": "email", "value": USERNAME}},
            {"tool": "wait_for", "text": "<tester2's username>"}]}))["text"]
        assert said.startswith("Ran 7 of 7 steps"), said
        assert_absent(USERNAME, said + looked)
        await finish(stop, task)
    run(go)


# ---- P2: a saved script keeps its structure ----
@pytest.mark.parametrize("value", ["tap", "format", "agent-tools-script", "steps", "label", "account"])
def test_a_value_that_is_a_tool_or_a_key_leaves_the_saved_script_whole(tmp_path, value):
    """Codex's trace: username `tap`, a recorded tap on id l1, stopped to a file. The file is a script that loads, its
    tool is `tap`, and only the screen's words are redacted."""
    async def go():
        d = daemon(tmp_path, username=value)
        d.session.buttons = [("l1", "Open"), ("l2", f"Hello {value}")]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "plain"})
        await d.run_tool("a", "tap", {"target": {"id": "l1"}, "look": False})
        path = tmp_path / "plain.json"
        said = (await d.run_tool("a", "record", {"stop": True, "path": str(path)}))["text"]
        assert said.startswith("Saved 1 ") and path.is_file(), said  # (the answer's words are redacted: `steps` too)
        data = batch.load_script(str(path))
        assert data["format"] == "agent-tools-script" and data["steps"] == [{"tool": "tap", "target": {"id": "l1"}}]
        await finish(stop, task)
    run(go)


def test_the_persistence_backstop_redacts_content_and_nothing_else():
    loaded = accountlib.Loaded(path="x", accounts={
        "t": accountlib.Account("t", "account", "hunter2-PW-9x"), "u": accountlib.Account("u", "tap", None)})
    route = routes.route("sign_in", BUNDLE, "welcome", "home", ["account", "name"], [
        {"tool": "tap", "target": {"label": "Say hunter2-PW-9x"}, "expect": {"text": "Hi hunter2-PW-9x", "timeout": 2}},
        {"tool": "type_text", "target": {"id": "email"}, "text": "${account.username}"},
        {"tool": "type_text", "target": {"id": "password"}, "text": "${account.password}"},
        {"tool": "type_text", "target": {"id": "note"}, "text": "${name} and hunter2-PW-9x and $${literal}"},
        {"tool": "compare_screen", "name": "tap", "ignore": [{"label": "tap here"}]}])
    saved = batch.redacted_content(route, loaded.scrub)
    batch.check_script(json.loads(json.dumps(saved)), "x")  # still a script: it loads
    steps = saved["steps"]
    assert [s["tool"] for s in steps] == ["tap", "type_text", "type_text", "type_text", "compare_screen"]
    assert saved["variables"] == ["account", "name"] and saved["format"] == "agent-tools-script"
    assert steps[0] == {"tool": "tap", "target": {"label": "Say <t's password>"},
                        "expect": {"text": "Hi <t's password>", "timeout": 2}}
    assert steps[1]["text"] == "${account.username}" and steps[2]["text"] == "${account.password}"
    assert steps[3]["text"] == "${name} and <t's password> and $${literal}"
    assert steps[4] == {"tool": "compare_screen", "name": "tap", "ignore": [{"label": "<u's username> here"}]}
    assert "hunter2-PW-9x" not in json.dumps(saved)


# ---- the map: an id or a title that holds a value is kept as it is shown ----
def test_a_screen_whose_id_and_title_hold_the_username_is_mapped_without_it(tmp_path):
    from tapwright import mapkeeper

    async def go():
        d = daemon(tmp_path)
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        d.session.buttons = [(USERNAME, "Profile of the tester"), ("l1", "Other")]
        d.session.texts = [USERNAME]
        stop, task = await started(d)
        said = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        said += (await d.run_tool("a", "map", {"name": "profile"}))["text"]
        said += (await d.run_tool("a", "tap", {"target": {"id": USERNAME}}))["text"]  # (the raw id still finds it)
        await d.maps.flush()
        written = "\n".join(p.read_text("utf-8") for p in (tmp_path / "maps").rglob("*") if p.is_file())
        assert written and "profile" in written
        assert_absent(USERNAME, said + written)
        await finish(stop, task)
    run(go)
