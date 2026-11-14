"""A test account's username or password is in no output: no answer, change list, audit line, recording, route, journey
report or trace has it in any form (AGENT-TOOLS §18.6; Codex, PR #10 cycle 2). The tree keeps the screen's own words
for the policy, matching and typing, and every edge redacts them (Codex, PR #10 cycle 3). The fake phone here builds its
tree from XML inside each call, as WebDriverAgent's /source is read, so the read is the real one."""
import json
from xml.etree import ElementTree as ET

import pytest
from mapfakes import APP, SCREEN
from test_daemon import finish, make, run, started
from test_daemon_accounts import BUNDLE, TypingSession, explore, folder, screens, write_accounts

from tapwright import audit, journey, mapkeeper, trace, ui
from tapwright import accounts as accountlib
from tapwright.actionlog import ActionLog

LONG_TAIL = "secret-long-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # Codex's finding 1: the change list's (was …)
VERY_LONG = "long-secret-" + "0123456789abcdef" * 15  # findings 1 (audit's id=) and 3 (journey reports)
QUOTED = 'synth"password42'  # finding 2: a recorded selector
PLACEHOLDER = "<tester2's password>"


def forms(value: str) -> set[str]:
    flat = " ".join(value.split())
    return {value, flat, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(flat, ensure_ascii=False)[1:-1]}


def assert_absent(value: str, text: str, where: str = "") -> None:
    """Neither the value nor any 12-character piece of it, raw, collapsed or JSON-escaped, is in `text`."""
    for form in forms(value):
        assert form not in text, (where, form)
        for i in range(max(1, len(form) - 11)):
            piece = form[i:i + 12]
            assert piece not in text, (where, piece)


def node(kind, **attrs):
    return ET.Element(f"XCUIElementType{kind}", {"type": f"XCUIElementType{kind}", "enabled": "true",
                                                 **{k: str(v) for k, v in attrs.items()}})


class RevealingSession(TypingSession):
    """The login screen read from XML on every call (as /source is): its email field shows what was typed (a revealed
    password too), and `bare` is a plain input with no label and no identifier that shows its value."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.bare: str | None = None

    async def tap(self, target, hold_ms=60):
        pending = trace.request("POST", "/session/S/wda/tw/tap")  # as Session's _call notes a touch for the trace
        out = await super().tap(target, hold_ms)
        trace.response(pending, {"value": {}})
        return out

    async def snapshot(self, keyboard=False, timeout=None):
        pending = trace.request("GET", "/source")  # (a read line too)
        snap = await super().snapshot(keyboard, timeout)
        trace.response(pending, {"value": "<xml/>"})
        trace.built()
        return snap

    def _show(self):
        if self.at != "login":
            return super()._show()
        app = node("Application", bundleId=BUNDLE, label=APP[0], x=0, y=0, width=400, height=800)
        bar = node("NavigationBar", name="Login", x=0, y=40, width=400, height=60)
        bar.append(node("StaticText", label="Login", name="Login", value="Login", x=10, y=50, width=200, height=30,
                        traits="StaticText,Header"))
        app.append(bar)
        for n, i in enumerate(("sign-in-btn", "l1", "l2")):
            app.append(node("Button", label=f"Label of {i}", name=i, x=10, y=620 + 44 * n, width=200, height=40))
        app.append(node("TextField", label="Field email", name="email", value=self.values.get("email", ""),
                        x=10, y=540, width=300, height=40))
        app.append(node("SecureTextField", label="Field password", name="password", value="", x=10, y=496, width=300,
                        height=40))
        if self.bare is not None:
            app.append(node("TextField", value=self.bare, x=10, y=440, width=300, height=40))
        tree = ui.read(ET.tostring(app, encoding="unicode"), SCREEN)  # (inside the call: the real ingest)
        self.items, self.last_tree = list(tree.items), tree
        self.last_insets = (tree.top_inset, tree.bottom_inset)
        self.last_alert, self.last_keyboard = tree.alert, tree.keyboard
        return tree


def revealing(tmp_path, password):
    session = RevealingSession(screens(), "welcome", {("welcome", "continue-btn"): "login", ("login", "sign-in-btn"): "home"})
    d = make(session)
    d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
    d.actions = ActionLog(tmp_path / "log")
    data = {"version": 1, "accounts": {"tester2": {"username": "tester2@example.com", "password": password, "apps": [BUNDLE]}}}
    d.ceiling = type("C", (), {"accounts": write_accounts(tmp_path, data, "ingest.json"), "brief": False, "maps": None})()
    return d


def bare_ref(text: str) -> str:
    line = next(line for line in text.splitlines() if " input " in line and PLACEHOLDER in line and "Field" not in line)
    return line.split()[0]


# ---- finding 1: the change list's (was …) and the audit's id= ----
def test_a_long_revealed_password_that_changes_is_in_no_change_list(tmp_path):
    async def go():
        d = revealing(tmp_path, LONG_TAIL)
        d.session.at = "login"
        stop, task = await started(d)
        d.session.values["email"] = LONG_TAIL  # the show-password toggle made the field plain text
        looked = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        assert PLACEHOLDER in looked
        d.session.values["email"] = LONG_TAIL + "x"  # one more key, then any action
        said = (await d.run_tool("a", "tap", {"target": {"id": "l1"}}))["text"]
        assert f'"{PLACEHOLDER}x"' in said, said  # the change is still told, in the placeholder's terms
        for text in (looked, said):
            assert_absent(LONG_TAIL, text)
        await finish(stop, task)
    run(go)


def test_an_audit_issue_on_an_unlisted_element_named_by_the_password_says_neither(tmp_path):
    async def go():
        d = revealing(tmp_path, VERY_LONG)
        d.session.at = "login"
        d.session.audit_issues = [{"auditType": audit.XCTEST["clipped_text"], "compactDescription": "Text is clipped",
                                   "detailedDescription": f"{VERY_LONG} is cut", "elementAttributes": {
                                       "type": "XCUIElementTypeStaticText", "name": VERY_LONG, "label": VERY_LONG,
                                       "rect": {"x": 5, "y": 300, "width": 50, "height": 20}}}]
        stop, task = await started(d)
        said = (await d.run_tool("a", "audit", {"types": ["clipped_text"]}))["text"]
        assert PLACEHOLDER in said
        assert_absent(VERY_LONG, said)
        await finish(stop, task)
    run(go)


# ---- finding 2: a recorded selector (a script, version 1, and a route, version 2) ----
def test_a_tap_by_ref_on_a_field_that_shows_the_password_is_recorded_without_it(tmp_path):
    async def go():
        d = revealing(tmp_path, QUOTED)
        d.session.at = "login"
        d.session.bare = QUOTED
        stop, task = await started(d)
        looked = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        await d.run_tool("a", "record", {"start": True, "name": "bare_tap"})
        await d.run_tool("a", "tap", {"target": {"ref": bare_ref(looked)}})
        await d.run_tool("a", "tap", {"target": {"text": PLACEHOLDER}})  # what the agent sees, as a selector
        script = tmp_path / "bare.json"
        said = (await d.run_tool("a", "record", {"stop": True, "path": str(script)}))["text"]
        saved = script.read_text("utf-8")
        assert_absent(QUOTED, saved + said)
        assert "password>" not in saved  # a placeholder is no selector either: it would find nothing on replay
        steps = json.loads(saved)["steps"]
        assert [set(s["target"]) for s in steps] == [{"x", "y"}, {"x", "y"}]  # no id, label or text that can be saved
        await finish(stop, task)
    run(go)


def test_a_recorded_route_through_a_field_that_shows_the_password_never_holds_it(tmp_path):
    async def go():
        d = revealing(tmp_path, QUOTED)
        stop, task = await started(d)
        await explore(d, with_route=False)
        await d.run_tool("a", "record", {"start": True, "route": "bare_route"})
        await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
        d.session.bare = QUOTED
        looked = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        await d.run_tool("a", "tap", {"target": {"ref": bare_ref(looked)}})
        await d.run_tool("a", "tap", {"target": {"id": "sign-in-btn"}})
        said = (await d.run_tool("a", "record", {"stop": True}))["text"]
        await d.maps.flush()
        everything = said
        for path in folder(tmp_path).rglob("*"):
            if path.is_file():
                everything += "\n" + path.read_text("utf-8", errors="replace")
        assert "bare_route" in everything
        assert_absent(QUOTED, everything)
        assert "password>" not in everything
        await finish(stop, task)
    run(go)


# ---- finding 3: a journey's report, its page and its JSON ----
def test_a_ux_check_finding_that_quotes_the_password_is_in_no_journey_file(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "reports_dir", lambda: tmp_path / "reports")

    async def go():
        d = revealing(tmp_path, VERY_LONG)
        d.session.at = "login"
        d.session.values["email"] = VERY_LONG
        d.session.audit_issues = [{"auditType": audit.XCTEST["clipped_text"],
                                   "compactDescription": f'Text "{VERY_LONG}" is clipped', "detailedDescription": "",
                                   "elementAttributes": {"type": "XCUIElementTypeTextField", "name": "email",
                                                         "label": "Field email",
                                                         "rect": {"x": 10, "y": 540, "width": 300, "height": 40}}}]
        stop, task = await started(d)
        await d.run_tool("a", "journey", {"start": True, "goal": "sign in", "eyes": "agent"})
        said = (await d.run_tool("a", "ux_check", {}))["text"]
        assert "is clipped" in said and PLACEHOLDER in said
        said += (await d.run_tool("a", "journey", {"stop": True, "reached": False}))["text"]
        (made,) = [p for p in (tmp_path / "reports").iterdir() if p.is_dir()]
        files = {name: (made / name).read_text("utf-8") for name in ("report.md", "report.html", "journey.json")}
        assert PLACEHOLDER in files["report.md"]  # the finding is filed, in the placeholder's terms
        for name, text in {**files, "answers": said}.items():
            assert_absent(VERY_LONG, text, name)
        await finish(stop, task)
    run(go)


# ---- the trace file and the action log ----
def test_the_trace_file_and_the_action_log_never_hold_a_revealed_value(tmp_path):
    path = tmp_path / "trace.jsonl"

    async def go():
        d = revealing(tmp_path, QUOTED)
        d.session.at = "login"
        d.session.bare = QUOTED
        stop, task = await started(d)
        trace.enable(path)
        try:
            looked = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
            await d.run_tool("a", "tap", {"target": {"ref": bare_ref(looked)}})
            await d.run_tool("a", "tap", {"target": {"text": PLACEHOLDER}})
            await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "plain words"})
        finally:
            trace.disable()
        written = path.read_text("utf-8")
        assert '"tap"' in written  # the touches were traced
        for log in (tmp_path / "log").rglob("*"):
            if log.is_file():
                written += log.read_text("utf-8", errors="replace")
        assert_absent(QUOTED, written)
        await finish(stop, task)
    run(go)


# ---- the tree keeps the screen's words; what it shows never does (Codex, PR #10 cycle 3) ----
def test_a_tree_read_keeps_the_words_and_everything_it_shows_redacts_them():
    loaded = accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", "user-name@example.com", QUOTED)})
    app = node("Application", bundleId=BUNDLE, label=APP[0], x=0, y=0, width=400, height=800)
    bar = node("NavigationBar", name=QUOTED, x=0, y=40, width=400, height=60)
    bar.append(node("StaticText", label=QUOTED, name=QUOTED, value=QUOTED, x=10, y=50, width=200, height=30, traits="Header"))
    app.append(bar)
    app.append(node("TextField", label="Email", name="user-name@example.com", value="user-name@example.com",
                    placeholderValue=QUOTED, x=10, y=200, width=300, height=40))
    box = node("Alert", label=f"Signed in as {QUOTED}", x=50, y=250, width=300, height=200)
    box.append(node("Button", label=QUOTED, name="OK", x=60, y=400, width=280, height=40))
    app.append(box)
    token = ui.REDACT.set(loaded.scrub)
    try:
        tree = ui.read(ET.tostring(app, encoding="unicode"), SCREEN)
        # inside: the screen's own words (the policy, matching and typing decide with them)
        held = repr([(e.label, e.identifier, e.value, e.name, e.placeholder) for e in tree.items]) + repr(tree.alert)
        assert QUOTED in held and "user-name@example.com" in held
        # outside: every line, the alert and the title as an answer shows them
        said = "\n".join([ui.describe(tree.items, SCREEN, 3.0, APP, tree.alert, tree.keyboard, 0, (0, 0)),
                          *(e.line() for e in tree.items), ui.clip(tree.structure.title or "")])
    finally:
        ui.REDACT.reset(token)
    assert "<t's password>" in said and "<t's username>" in said
    assert_absent(QUOTED, said)
    assert_absent("user-name@example.com", said)


def test_a_scrub_is_idempotent_even_when_the_name_holds_the_value():
    loaded = accountlib.Loaded(path="x", accounts={"tester": accountlib.Account("tester", None, "test")})
    once = loaded.scrub("my test")
    assert once == "my <tester's password>" and loaded.scrub(once) == once
    assert loaded.holds(once) and loaded.holds("test") and not loaded.holds("my words")


def test_a_selector_that_holds_a_value_or_its_placeholder_is_never_recorded():
    from tapwright import batch
    loaded = accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", None, QUOTED)})
    token = ui.REDACT.set(loaded.scrub)
    try:
        field = ui.Element("e1", "input", "", value=QUOTED, rect=(10, 100, 100, 40), actions=("tap", "type"), type="TextField")
        named = ui.Element("e2", "input", "Password shown", "pw-field", value=QUOTED, rect=(10, 200, 100, 40), type="TextField")
        items = [field, named]
        assert batch.recorded_target({"ref": "e1"}, items) == {"x": 60.0, "y": 120.0}
        # the placeholder an answer shows, and the value itself, find the field (and the other that shows it): neither
        # is saved, the point is
        assert batch.recorded_target({"text": "<t's password>", "index": 0}, items) == {"x": 60.0, "y": 120.0}
        assert batch.recorded_target({"text": QUOTED, "index": 0}, items) == {"x": 60.0, "y": 120.0}
        assert batch.recorded_target({"ref": "e2"}, items) == {"id": "pw-field"}
        with pytest.raises(Exception) as e:
            batch.recorded_ignore([{"ref": "e1"}], items)
        assert "account" in str(e.value)
    finally:
        ui.REDACT.reset(token)
