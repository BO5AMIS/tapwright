"""Codex, PR #10 cycle 8: percent escapes in any case, a toggle that changes nothing recorded by its switch, a recorded
selector counted as the replaying tool counts, and typed account values recorded as the variables the tools type."""
import json
from io import StringIO

import pytest
from test_account_values_at_ingest import assert_absent
from test_account_values_raw_inside import USERNAME, daemon
from test_daemon import finish, make, run, started
from test_diagnostics import opened, tuned
from test_pr10_cycle4 import logging_to, with_accounts
from test_pr10_cycle7 import HEADER, pull_with

from tapwright import accounts as accountlib
from tapwright import diagnostics, ui
from tapwright.control import Refused

SECRET = "synth/secret?42"


# ---- 1: percent escapes in any case ----
@pytest.mark.parametrize("escaped", ["synth%2fsecret%3f42", "synth%2Fsecret%3f42", "synth%2fsecret%3F42", "synth%2Fsecret%3F42"])
def test_a_percent_encoded_value_is_scrubbed_whatever_the_case_of_its_escapes(escaped):
    loaded = accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", None, SECRET)})
    assert loaded.holds(f"https://example.invalid/?token={escaped}&x=1")
    scrubbed = loaded.scrub(f"https://example.invalid/?token={escaped}&x=1")
    assert scrubbed == "https://example.invalid/?token=<t's password>&x=1"  # (the value alone: cycle 10)


def test_only_the_escapes_are_case_insensitive():
    loaded = accountlib.Loaded(path="x", accounts={"t": accountlib.Account("t", None, "Abc-Secret")})
    assert loaded.scrub("abc-secret ABC-SECRET Abc-Secret") == "abc-secret ABC-SECRET <t's password>"
    assert loaded.scrub("Abc%2dSecret") == "<t's password>"
    assert loaded.scrub("abc%2dsecret") == "abc%2dsecret"


def test_the_crash_export_scrubs_lowercase_escapes(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)
    body = '{"url":"https://example.invalid/?token=synth%2fsecret%3f42&b=synth%2Fsecret%3f42"}'
    answer = pull_with(tmp_path, f"{HEADER}{body}\n".encode(), "tester2@example.com", SECRET)
    path = next((tmp_path / "crashes").rglob("*.ips"))
    raw = path.read_text("utf-8")
    assert f"Saved: {path}" in answer
    assert "secret%3" not in raw.lower() and "synth%2" not in raw.lower()
    assert "<tester2's password>" in raw


def test_the_log_filter_scrubs_a_lowercase_encoded_value(tmp_path):
    out = StringIO()
    d = with_accounts(make(), tmp_path, password=SECRET)
    log = logging_to(out, d)
    log.info("GET /?token=synth%2fsecret%3f42")
    assert "secret%3" not in out.getvalue().lower() and "<tester2's password>" in out.getvalue()


# ---- 2 and 3: toggles ----
def tapped_rects(d) -> list:
    rects: list = []
    real = d.session.tap

    async def tap(target, hold_ms=60):
        hit = ui.resolve(d.session.items, target)
        rects.append(hit.rect if isinstance(hit, ui.Element) else hit)
        return await real(target, hold_ms)
    d.session.tap = tap
    return rects


def test_a_toggle_that_changes_nothing_is_recorded_as_the_switch_it_resolved(tmp_path):
    async def go():
        d = daemon(tmp_path)
        d.session.headings = [("account-heading", USERNAME)]
        d.session.switches = [("account-switch", USERNAME)]
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "nop"})
        said = (await d.run_tool("a", "toggle", {"target": {"label": "<tester2's username>", "index": 0},
                                                 "value": False, "look": False}))["text"]
        assert "already false" in said
        script = tmp_path / "nop.json"
        assert (await d.run_tool("a", "record", {"stop": True, "path": str(script)}))["text"].startswith("Saved 1 steps")
        steps = json.loads(script.read_text("utf-8"))["steps"]
        assert steps == [{"tool": "toggle", "value": False, "target": {"id": "account-switch"}}], steps
        r = await d.run_tool("a", "replay", {"path": str(script)})
        assert "Ran 1 of 1 steps" in r["text"], r["text"]
        await finish(stop, task)
    run(go)


def test_a_recorded_toggle_index_counts_the_switches_as_replay_does(tmp_path):
    async def go():
        d = daemon(tmp_path)
        d.session.headings = [("shared-control", "Account")]
        d.session.switches = [("shared-control", USERNAME), ("shared-control", "Other account")]
        stop, task = await started(d)
        rects = tapped_rects(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "ix"})
        await d.run_tool("a", "toggle", {"target": {"label": "<tester2's username>"}, "look": False})
        first = rects[-1]
        script = tmp_path / "ix.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        steps = json.loads(script.read_text("utf-8"))["steps"]
        assert steps == [{"tool": "toggle", "target": {"id": "shared-control", "index": 0}}], steps
        assert_absent(USERNAME, script.read_text("utf-8"))
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "replay", {"path": str(script)})
        assert rects[-1] == first and len(rects) == 2
        await finish(stop, task)
    run(go)


def test_a_selector_that_the_replaying_tool_would_resolve_elsewhere_is_not_saved_as_is():
    from tapwright import batch
    items = ui.read(_xml(), _SCREEN()).items
    switches = [e for e in items if e.role == "switch"]
    aim = batch.recorded_element(switches[1], items, "toggle")
    assert aim == {"id": "shared-control", "index": 1}
    assert ui.replayed(items, ui.parse_target(aim), "toggle") is switches[1]
    assert batch.recorded_element(switches[1], items) == {"id": "shared-control", "index": 2}  # (tap counts all three)


def _SCREEN():
    from mapfakes import SCREEN
    return SCREEN


def _xml() -> str:
    from xml.etree import ElementTree as ET

    from mapfakes import APP
    from test_account_values_raw_inside import BUNDLE, node
    app = node("Application", bundleId=BUNDLE, label=APP[0], x=0, y=0, width=400, height=800)
    app.append(node("StaticText", label="A", name="shared-control", value="A", x=10, y=100, width=300, height=20, traits="StaticText"))
    for n, label in enumerate(("One", "Two")):
        app.append(node("Switch", label=label, name="shared-control", value="0", x=300, y=130 + 44 * n, width=51, height=31))
    return ET.tostring(app, encoding="unicode")


# ---- 4: typed account values ----
from test_daemon_accounts import PASSWORD, USER, explore, folder, mapped, typed  # noqa: E402


def test_a_typed_username_and_password_are_recorded_as_variables_and_the_route_types_them(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d, with_route=False)
        await d.run_tool("a", "record", {"start": True, "route": "typed_in"})
        await d.run_tool("a", "tap", {"target": {"id": "continue-btn"}})
        await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": USER, "look": False})
        await d.run_tool("a", "type_text", {"target": {"id": "password"}, "text": PASSWORD, "look": False})
        await d.run_tool("a", "tap", {"target": {"id": "sign-in-btn"}})
        said = (await d.run_tool("a", "record", {"stop": True}))["text"]
        await d.maps.flush()
        saved = (folder(tmp_path) / "routes" / "typed_in.json").read_text("utf-8")
        data = json.loads(saved)
        assert [s.get("text") for s in data["steps"] if s["tool"] == "type_text"] == ["${account.username}", "${account.password}"]
        assert "account" in data["variables"]
        assert "<tester2's" not in saved
        assert_absent(USER, saved + said)
        assert_absent(PASSWORD, saved + said)
        d.session.at, d.session.typed, d.session.values = "welcome", [], {}
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "drive", {"route": "typed_in", "account": "tester2"})
        assert r["text"].startswith("Arrived at home"), r["text"]
        assert typed(d) == [USER, PASSWORD]
        await finish(stop, task)
    run(go)


def test_a_typed_username_in_a_script_is_saved_as_the_variable(tmp_path):
    async def go():
        d = daemon(tmp_path)
        d.session.field = ""
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "ty"})
        await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": USERNAME, "submit": True, "look": False})
        script = tmp_path / "ty.json"
        said = (await d.run_tool("a", "record", {"stop": True, "path": str(script)}))["text"]
        assert said.startswith("Saved 1 steps"), said
        steps = json.loads(script.read_text("utf-8"))["steps"]
        assert steps[0]["text"] == "${account.username}" and steps[0]["submit"] is True, steps
        assert "<tester2's" not in script.read_text("utf-8")
        assert_absent(USERNAME, script.read_text("utf-8"))
        await finish(stop, task)
    run(go)


@pytest.mark.parametrize("text", [f"hi {USERNAME}", f"{USERNAME}!", "<tester2's username>", "pw is pw-secret-0042"])
def test_typed_text_that_only_holds_a_value_is_refused_naming_the_step_not_the_value(tmp_path, text):
    async def go():
        d = daemon(tmp_path)
        d.session.field = ""
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "ty"})
        await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": text, "look": False})
        script = tmp_path / "ty.json"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        assert e.value.code == "bad_params" and "step 1" in str(e.value)
        assert_absent(USERNAME, str(e.value))
        assert not script.exists()
        await finish(stop, task)
    run(go)


def test_other_typed_text_is_saved_as_typed(tmp_path):
    async def go():
        d = daemon(tmp_path)
        d.session.field = ""
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "ty"})
        await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "someone@example.org", "look": False})
        script = tmp_path / "ty.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(script)})
        assert json.loads(script.read_text("utf-8"))["steps"][0]["text"] == "someone@example.org"
        await finish(stop, task)
    run(go)
