"""Test accounts through the daemon (AGENT-TOOLS §18.6): typed by drive, into the right fields, and shown nowhere."""
import dataclasses
import json
import shutil
import subprocess

import pytest
from mapfakes import APP, MapSession, screen
from test_daemon import finish, make, run, started

from tapwright import mapkeeper, routes
from tapwright.actionlog import ActionLog
from tapwright.control import Refused

BUNDLE = APP[1]
USER, PASSWORD, PASSCODE = "tester2@example.com", "hunter2-PW-9x", "482913"
ACCOUNTS = {"version": 1, "accounts": {
    "tester2": {"username": USER, "password": PASSWORD, "apps": [BUNDLE]},
    "tester1": {"username": "tester1@example.com", "password": "correct-horse-7", "apps": ["com.other.app"]},
    "phone": {"passcode": PASSCODE}}}
GOES = {("welcome", "continue-btn"): "login", ("login", "sign-in-btn"): "home"}
SIGN_IN = [{"tool": "tap", "target": {"id": "continue-btn"}},
           {"tool": "type_text", "target": {"id": "email"}, "text": "${account.username}"},
           {"tool": "type_text", "target": {"id": "password"}, "text": "${account.password}"},
           {"tool": "tap", "target": {"id": "sign-in-btn"}}]


class TypingSession(MapSession):
    """A phone whose fields show what was typed into them (a password's value isn't shown, as on the phone)."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.values: dict[str, str] = {}

    def _show(self):
        tree = super()._show()
        items = [dataclasses.replace(e, value=self.values[e.identifier]) if e.identifier in self.values and not e.secure else e
                 for e in tree.items]
        shown = dataclasses.replace(tree, items=items)
        self.items, self.last_tree = list(items), shown
        return shown

    async def type_text(self, text, replace=False, submit=False, target=None, at=None):
        out = await super().type_text(text, replace, submit, target, at)
        hit = ui_resolve(self.items, target)
        self.values[hit.identifier] = text
        return out


def ui_resolve(items, target):
    from tapwright import ui
    return ui.resolve(items, target)


def screens():
    return {"welcome": screen("Welcome", ids=("continue-btn", "w1", "w2")),
            "login": screen("Login", ids=("sign-in-btn", "l1", "l2"), inputs=("email",), secure=("password",)),
            "home": screen("Home", ids=("settings-btn", "h1", "h2"), tab="home")}


def write_accounts(tmp_path, data=ACCOUNTS, name="accounts.json"):
    path = tmp_path / "private" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data), "utf-8")
    return str(path)


def mapped(tmp_path, accounts=True, goes=None):
    session = TypingSession(screens(), "welcome", {**GOES, **(goes or {})})
    d = make(session)
    d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
    d.actions = ActionLog(tmp_path / "log")
    d.ceiling = type("C", (), {"accounts": write_accounts(tmp_path) if accounts else None, "brief": False, "maps": None})()
    return d


def folder(tmp_path):
    return tmp_path / "maps" / BUNDLE


async def explore(d, with_route=True):
    await d.run_tool("a", "look", {"screenshot": False})
    await d.run_tool("a", "map", {"name": "welcome"})
    await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
    await d.run_tool("a", "map", {"name": "login"})
    await d.run_tool("a", "tap", {"target": {"id": "sign-in-btn"}})
    await d.run_tool("a", "map", {"name": "home"})
    d.session.at = "welcome"
    d.session.values.clear()
    await d.run_tool("a", "look", {"screenshot": False})
    if with_route:
        d.maps.file(None, BUNDLE).write_route("sign_in", routes.route("sign_in", BUNDLE, "welcome", "home", ["account"], SIGN_IN))
        d.maps.edit(None, BUNDLE, lambda data: data["routes"].__setitem__("sign_in", "routes/sign_in.json"))


def typed(d):
    """What was typed, and into which field (the field the phone's screen holds it for: a password isn't shown)."""
    return [t[0] for t in d.session.typed]


def test_drive_types_the_username_and_the_password_into_the_right_fields(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        r = await d.run_tool("a", "drive", {"route": "sign_in", "account": "tester2"})
        assert r["text"].startswith("Arrived at home by route sign_in in ")
        assert typed(d) == [USER, PASSWORD] and d.session.values == {"email": USER, "password": PASSWORD}
        await finish(stop, task)
    run(go)


def test_the_values_are_nowhere_in_an_answer_a_file_or_the_log(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        said = []
        await d.run_tool("a", "record", {"start": True, "name": "login_script"})
        said.append((await d.run_tool("a", "drive", {"route": "sign_in", "account": "tester2", "look": True}))["text"])
        d.session.at = "login"
        d.session.values.update({"email": USER})
        said.append((await d.run_tool("a", "look", {"screenshot": False}))["text"])  # the field shows its value on the phone
        said.append((await d.run_tool("a", "wait_for", {"id": "email"}))["text"])
        said.append((await d.run_tool("a", "map", {}))["text"])
        said.append((await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "email"}}]}))["text"])
        said.append((await d.run_tool("a", "session_report", {}))["text"])
        script = tmp_path / "script.json"
        said.append((await d.run_tool("a", "record", {"stop": True, "path": str(script)}))["text"])
        with pytest.raises(Refused) as e:  # an error carries the screen too
            await d.run_tool("a", "drive", {"route": "sign_in", "account": "tester2", "dry_run": False, "with": {}, "then": [
                {"tool": "tap", "target": {"id": "nope"}}]})
        said.append(str(e.value))
        await d.maps.flush()
        assert any(p.is_file() for p in (tmp_path / "log").rglob("*")), "the log is read too"
        everything ="\n".join(said)
        for path in [*folder(tmp_path).rglob("*"), *(tmp_path / "log").rglob("*"), script]:
            if path.is_file():
                everything += "\n" + path.read_text("utf-8", errors="replace")
        for secret in (USER, PASSWORD, PASSCODE, "tester1@example.com", "correct-horse-7"):
            assert secret not in everything, secret
        assert "<tester2's username>" in "\n".join(said[1:3])  # a field that holds the username says whose it is
        saved = script.read_text("utf-8")
        assert "${account.username}" in saved and "${account.password}" in saved  # a recording keeps the placeholders
        await finish(stop, task)
    run(go)


def test_a_password_goes_only_into_a_secure_field_and_a_username_only_into_a_plain_one(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d, with_route=False)
        swapped = [SIGN_IN[0], {**SIGN_IN[1], "target": {"id": "password"}}, SIGN_IN[2], SIGN_IN[3]]
        d.maps.file(None, BUNDLE).write_route("swapped", routes.route("swapped", BUNDLE, "welcome", "home", ["account"], swapped))
        d.maps.edit(None, BUNDLE, lambda data: data["routes"].__setitem__("swapped", "routes/swapped.json"))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"route": "swapped", "account": "tester2"})
        assert e.value.code == "bad_params"
        assert "an account's username goes only into a text field that isn't secure; " in str(e.value) and "Done:" in str(e.value)
        assert d.session.typed == []
        pw_in_text = [SIGN_IN[0], {**SIGN_IN[2], "target": {"id": "email"}}]
        d.maps.file(None, BUNDLE).write_route("plain_pw", routes.route("plain_pw", BUNDLE, "welcome", "home", ["account"], pw_in_text))
        d.maps.edit(None, BUNDLE, lambda data: data["routes"].__setitem__("plain_pw", "routes/plain_pw.json"))
        d.session.at = "welcome"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"route": "plain_pw", "account": "tester2"})
        assert "an account's password goes only into a password field; " in str(e.value) and "is a text field." in str(e.value)
        assert d.session.typed == []
        await finish(stop, task)
    run(go)


def test_a_value_is_never_part_of_a_text(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d, with_route=False)
        d.session.at = "login"
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"to": "login", "account": "tester2",
                                            "then": [{"tool": "type_text", "target": {"id": "password"}, "text": "hi ${account.password}"}]})
        assert e.value.code == "bad_params" and "whole text" in str(e.value)
        assert d.session.typed == []
        await finish(stop, task)
    run(go)


def test_without_drive_there_is_no_account_to_type(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        d.session.at = "login"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"target": {"id": "password"}, "text": "${account.password}"})
        assert e.value.code == "bad_params" and "drive" in str(e.value)
        with pytest.raises(Refused):
            await d.run_tool("a", "run", {"steps": [{"tool": "type_text", "target": {"id": "password"}, "text": "${account.password}"}]})
        assert d.session.typed == []
        await finish(stop, task)
    run(go)


def test_the_then_steps_of_a_drive_may_type_an_account(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d, with_route=False)
        d.session.at = "login"
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "drive", {"to": "login", "account": "tester2",
                                        "then": [{"tool": "type_text", "target": {"id": "password"}, "text": "${account.password}"}]})
        assert typed(d) == [PASSWORD] and d.session.values == {"password": PASSWORD}
        await finish(stop, task)
    run(go)


def test_an_accounts_value_is_not_typed_into_an_app_it_isnt_for_even_after_a_step_switched_apps(tmp_path):
    async def go():
        d = mapped(tmp_path)
        d.session.installed.append({"bundle_id": "com.other.app", "name": "Other", "version": "1", "build": "1", "executable": "Other"})
        stop, task = await started(d)
        await explore(d)
        switch = {"tool": "open_app", "app": "com.other.app"}
        d.session.at = "login"
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:  # a then step
            await d.run_tool("a", "drive", {"to": "login", "account": "tester2", "then": [
                switch, {"tool": "type_text", "target": {"id": "password"}, "text": "${account.password}"}]})
        assert e.value.code == "not_allowed" and "not for this app" in str(e.value) and PASSWORD not in str(e.value)
        assert d.session.typed == [] and d.session.foreground == "com.other.app"
        # a route's own step
        d.session.foreground, d.session.app, d.session.at = BUNDLE, ("Example", BUNDLE), "welcome"
        await d.run_tool("a", "look", {"screenshot": False})
        steps = [SIGN_IN[0], switch, SIGN_IN[1]]
        d.maps.file(None, BUNDLE).write_route("hop", routes.route("hop", BUNDLE, "welcome", "home", ["account"], steps))
        d.maps.edit(None, BUNDLE, lambda data: data["routes"].__setitem__("hop", "routes/hop.json"))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"route": "hop", "account": "tester2"})
        assert e.value.code == "not_allowed"
        assert d.session.typed == []
        await finish(stop, task)
    run(go)


def test_an_accounts_value_in_an_element_is_scrubbed_from_the_live_view_and_the_log(tmp_path):
    async def go():
        d = mapped(tmp_path)
        d.session.screens["welcome"] = screen("Welcome", ids=(USER, "w1"))
        stop, task = await started(d)
        queue = d.subscribe()
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "tap", {"target": {"id": USER}})
        sent = []
        while not queue.empty():
            sent.append(json.dumps(queue.get_nowait()))
        shown = "\n".join(sent)
        assert '"type": "action"' in shown and "<tester2's username>" in shown
        assert USER not in shown
        logged = "\n".join(p.read_text("utf-8", errors="replace") for p in (tmp_path / "log").rglob("*") if p.is_file())
        assert '"tool": "tap"' in logged and "<tester2's username>" in logged
        assert USER not in logged
        await finish(stop, task)
    run(go)


def test_the_account_is_checked_before_anything_is_touched(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        before = d.session.calls.count("tap")
        for account, code in (("nobody", "not_found"), ("tester1", "not_allowed"), ("phone", "not_found")):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "drive", {"route": "sign_in", "account": account})
            assert e.value.code == code, account
        with pytest.raises(Refused) as e:  # a route that wants an account, given none
            await d.run_tool("a", "drive", {"route": "sign_in"})
        assert e.value.code == "bad_params" and "account" in str(e.value)
        assert d.session.calls.count("tap") == before
        await finish(stop, task)
    run(go)


def test_no_file_means_no_accounts(tmp_path):
    async def go():
        d = mapped(tmp_path, accounts=False)
        stop, task = await started(d)
        await explore(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"route": "sign_in", "account": "tester2"})
        assert e.value.code == "bad_params" and "--accounts" in str(e.value)
        await finish(stop, task)
    run(go)


def test_the_map_lists_the_account_names_and_the_report_does_too(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        assert "Accounts: tester2, tester1." in (await d.run_tool("a", "map", {}))["text"]
        assert "Accounts: tester2, tester1." in (await d.run_tool("a", "session_report", {}))["text"]
        await finish(stop, task)
    run(go)


def test_the_passcode_is_stored_and_used_nowhere(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        assert d._loaded().passcode == PASSCODE
        assert d.session.typed == [] and d.session.calls.count("key:enter") == 0
        await finish(stop, task)
    run(go)


GIT = shutil.which("git")


@pytest.mark.skipif(GIT is None, reason="git isn't installed here")
def test_a_file_in_a_git_repo_that_doesnt_ignore_it_starts_but_refuses_any_use_and_says_so(tmp_path):
    async def go():
        d = mapped(tmp_path)
        subprocess.run(["git", "init", "-q", str(tmp_path / "private")], check=True)
        stop, task = await started(d)
        first = await d.run_tool("a", "look", {"screenshot": False})
        assert "Note: accounts file is inside the git repo at " in first["text"]
        assert "and not ignored; move it or add it to .gitignore." in first["text"]
        await explore(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "drive", {"route": "sign_in", "account": "tester2"})
        assert e.value.code == "bad_params" and str(e.value).startswith("accounts file is inside the git repo at ")
        assert "Accounts:" not in (await d.run_tool("a", "map", {}))["text"]
        await finish(stop, task)
    run(go)


def test_a_road_never_keeps_a_name_that_is_an_accounts_value(tmp_path):
    d = mapped(tmp_path)
    loaded = d._loaded()
    assert loaded.clean({"id": "email", "text": USER, "label": "Email"}) == {"id": "email", "label": "Email"}
