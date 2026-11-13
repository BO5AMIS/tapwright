"""Codex, PR #10 cycle 4: a test account's value is kept out of every sink (diagnostics before any cut, the daemon's log,
the maps' tab id, repaired roads and the map writer's backstop, the direction-mark-free form) (AGENT-TOOLS §18.6)."""
import json
import logging
from io import StringIO
from xml.etree import ElementTree as ET

import pytest
from mapfakes import APP, SCREEN, MapSession, screen
from test_account_values_at_ingest import assert_absent
from test_account_values_raw_inside import USERNAME, daemon as account_daemon, node
from test_daemon import finish, make, run, started
from test_daemon_accounts import BUNDLE, write_accounts
from test_diagnostics import BASE, opened, settle, tuned

from tapwright import accounts as accountlib
from tapwright import diagnostics, mapfile, mapkeeper, places, server, ui
from tapwright.control import Refused
from tapwright.device import DeviceError

PW = "secret-account-0123456789"


def with_accounts(d, tmp_path, username="tester2@example.com", password=PW):
    data = {"version": 1, "accounts": {"tester2": {"username": username, "password": password, "apps": [BUNDLE]}}}
    d.ceiling = type("C", (), {"accounts": write_accounts(tmp_path, data, "c4.json"), "brief": False, "maps": None})()
    return d


# ---- 1: diagnostics are redacted before any cut ----
def test_a_log_line_with_the_password_across_the_300_and_2000_character_cuts_shows_and_keeps_none_of_it(tmp_path):
    async def go():
        d = with_accounts(tuned(make()), tmp_path)
        stop, task = await started(d)
        await opened(d)
        device = d.session.device
        device.emit(42, BASE + 1, "x" * 290 + PW)
        device.emit(42, BASE + 2, "y" * 1990 + PW + " tail")
        await settle(lambda: len(d._rings[BUNDLE].entries) == 2)
        said = (await d.run_tool("a", "logs", {}))["text"]
        assert "<tester2's" in said
        kept = "\n".join(e.text for e in d._rings[BUNDLE].entries)
        assert_absent(PW, said, "answer")
        assert_absent(PW, kept, "ring")
        await finish(stop, task)
    run(go)


def test_the_cut_and_the_failure_lines_redact_first_under_a_calls_accounts(tmp_path):
    loaded = accountlib.load(write_accounts(tmp_path, {"version": 1, "accounts": {"t": {"password": PW}}}, "u.json"))
    token = ui.REDACT.set(loaded.scrub)
    try:
        assert_absent(PW, diagnostics.cut("x" * 290 + PW))
        ring = diagnostics.LogRing()
        ring.add(diagnostics.LogEntry(BASE, "error", "z" * 295 + PW))
        assert_absent(PW, "\n".join(diagnostics.failure_logs(ring, 2, BASE)))
    finally:
        ui.REDACT.reset(token)


def test_a_crash_reason_is_redacted_before_it_is_cut(tmp_path):
    loaded = accountlib.load(write_accounts(tmp_path, {"version": 1, "accounts": {"t": {"password": PW}}}, "u.json"))

    class Report:
        exception_subtype = None
        application_specific_information = "z" * 190 + PW
        exception_type = "EXC_CRASH"
        frames: list = []

    info = diagnostics.describe_crash(Report(), "Example", loaded.scrub)
    assert_absent(PW, info.reason)
    assert info.reason.startswith("z" * 190)


# ---- 2: the daemon's log ----
def logging_to(stream: StringIO, d) -> logging.Logger:
    log = logging.getLogger(f"tw-c4-{id(stream)}")
    log.propagate = False
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(server.RedactAccounts(d.scrub_everywhere))
    log.addHandler(handler)
    return log


@pytest.mark.parametrize("error", [DeviceError(f"element error: {PW}"), RuntimeError(f"boom {PW}")])
def test_the_daemon_log_holds_no_account_value_whatever_a_tool_raised(tmp_path, error):
    async def go():
        out = StringIO()
        d = make(MapSession({"only": screen("Only", ids=("b1",))}, "only"), log=lambda m: None)
        with_accounts(d, tmp_path)
        d.log = lambda message: log.info(message)  # (the production wiring: the server's logger's `info`)
        log = logging_to(out, d)
        stop, task = await started(d)
        d.session.fail["tap"] = error
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "tap", {"target": {"id": "b1"}})
        assert_absent(PW, str(e.value))
        await finish(stop, task)
        text = out.getvalue()
        assert text and "failed" in text
        assert_absent(PW, text, "log")
    run(go)


def test_the_filter_redacts_a_record_logged_outside_any_call_and_its_traceback(tmp_path):
    out = StringIO()
    d = with_accounts(make(), tmp_path)
    log = logging_to(out, d)
    try:
        raise ValueError(f"bad {PW}")
    except ValueError:
        log.exception("trace of %s", PW)
    log.info("also %s", PW)
    assert "trace of" in out.getvalue() and "Traceback" in out.getvalue()
    assert_absent(PW, out.getvalue())


def test_the_servers_log_file_is_guarded(tmp_path):
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    d = with_accounts(make(), tmp_path)
    try:
        log = server.configure_logging(tmp_path / "serve.log")
        server.redact_log(d.scrub_everywhere)
        log.info("password %s", PW)
        for handler in root.handlers:
            handler.flush()
        written = (tmp_path / "serve.log").read_text("utf-8")
    finally:
        for handler in list(root.handlers):
            handler.close()
            root.removeHandler(handler)
        for handler in saved:
            root.addHandler(handler)
        root.setLevel(level)
    assert "password" in written
    assert_absent(PW, written)


# ---- 3, 4: the maps ----
class TabPhone(MapSession):
    def __init__(self, tab_id: str) -> None:
        app = node("Application", bundleId=BUNDLE, label=APP[0], x=0, y=0, width=400, height=800)
        bar = node("NavigationBar", name="Home", x=0, y=40, width=400, height=60)
        bar.append(node("StaticText", label="Home", name="Home", value="Home", x=10, y=50, width=200, height=30,
                        traits="StaticText,Header"))
        app.append(bar)
        app.append(node("Button", label="Go", name="go-btn", x=10, y=300, width=200, height=40))
        tabs = node("TabBar", x=0, y=740, width=400, height=60)
        tabs.append(node("Button", label="Home", name=tab_id, x=20, y=745, width=120, height=50, traits="Selected"))
        tabs.append(node("Button", label="Settings", name="tab-settings", x=170, y=745, width=120, height=50))
        app.append(tabs)
        super().__init__({"only": ui.read(ET.tostring(app, encoding="unicode"), SCREEN)}, "only")


def test_the_selected_tab_id_that_is_an_account_value_is_not_in_the_map(tmp_path):
    async def go():
        d = with_accounts(make(TabPhone(PW)), tmp_path)
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        stop, task = await started(d)
        said = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        await d.maps.flush()
        written = "\n".join(p.read_text("utf-8") for p in (tmp_path / "maps").rglob("*.json"))
        assert BUNDLE in str(next((tmp_path / "maps").iterdir())) and "tab" in written
        assert_absent(PW, said + written)
        await finish(stop, task)
    run(go)


def test_a_repaired_road_whose_new_target_is_the_username_is_not_saved_with_it(tmp_path):
    from test_daemon_drive import file_of, learn, mapped

    async def go():
        (tmp_path / "acc").mkdir()
        d = with_accounts(mapped(tmp_path), tmp_path / "acc", username=USERNAME)
        stop, task = await started(d)
        await learn(d)
        settings = screen("Settings", ids=("general-row", "about-row", "s1", "s2", "s3"))
        button = next(e for e in settings.items if e.identifier == "general-row")
        button.identifier = button.name = USERNAME  # the id changed to a value; the old label stays
        d.session.screens["settings"] = settings
        d.session.goes[("settings", USERNAME)] = "general"
        r = await d.run_tool("a", "drive", {"to": "language"})
        assert r["text"].startswith("Arrived at language")
        assert_absent(USERNAME, r["text"])
        await d.maps.flush()
        written = "\n".join(p.read_text("utf-8") for p in tmp_path.rglob("*") if p.is_file() and "acc" not in p.parts)
        assert_absent(USERNAME, written)
        assert {x["move"]["target"]["id"] for x in file_of(tmp_path)["roads"]} == {"settings-btn", "general-row", "language-row"}
        await finish(stop, task)
    run(go)


def test_the_map_writer_refuses_a_value_that_holds_an_account_and_never_touches_a_key(tmp_path):
    loaded = accountlib.load(write_accounts(tmp_path, {"version": 1, "accounts": {"tab": {"username": PW}}}, "w.json"))
    data = places.empty_map(BUNDLE)
    key = {"signature": "abc", "parts": {"ids": ["fine"], "kinds": [], "tab": PW, "layer": None}}
    data["places"] = {"home": {"key": key, "titles": {}}, "tab": {"key": {"signature": "d", "parts": {"ids": [PW]}}, "titles": {}}}
    data["roads"] = [{"from": "home", "to": "tab", "move": {"tool": "tap", "target": {"id": PW}}, "names": {}},
                     {"from": "home", "to": "tab", "move": {"tool": "tap", "target": {"id": "ok"}}, "names": {"label": "OK"}}]
    file = mapfile.MapFile(tmp_path / "maps", BUNDLE, loaded.scrub)
    file.save(data)
    text = file.path.read_text("utf-8")
    assert_absent(PW, text)
    written = json.loads(text)
    assert "tab" in written["places"] and "tab" in written["places"]["home"]["key"]["parts"]  # (keys stay)
    assert [r["move"]["target"] for r in written["roads"]] == [{"id": "ok"}]  # the road that held one is dropped


# ---- 7: direction marks ----
def test_a_password_with_a_direction_mark_is_redacted_in_the_form_the_screen_text_has_it(tmp_path):
    marked = f"pw-abc{chr(0x200e)}def-4567"
    plain = "pw-abcdef-4567"

    async def go():
        d = account_daemon(tmp_path, password=marked)
        d.session.field = marked  # revealed in a plain field
        d.session.buttons = [(plain, plain)]
        d.session.texts = [plain]
        stop, task = await started(d)
        looked = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        assert "password" in looked and plain not in looked and marked not in looked, looked
        ref = next(line.split()[0] for line in looked.splitlines() if " button " in line)
        await d.run_tool("a", "record", {"start": True, "name": "bidi"})
        await d.run_tool("a", "tap", {"target": {"ref": ref}, "look": False})
        script = tmp_path / "bidi.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        saved = script.read_text("utf-8")
        assert "abcdef" not in saved and "abc" not in saved
        assert set(json.loads(saved)["steps"][0]["target"]) == {"x", "y"}
        await finish(stop, task)
    run(go)


def test_the_marked_and_plain_forms_are_both_the_accounts_values():
    assert {"pw-abc‎def-4567", "pw-abcdef-4567"} <= accountlib._forms("pw-abc‎def-4567")
