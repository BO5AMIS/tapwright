"""`ux_check` and the house rules (AGENT-TOOLS §17.3)."""
import subprocess
import sys

import pytest
from starlette.testclient import TestClient

from tapwright import audit, client, uxcheck
from tapwright.control import Controller, Refused
from tapwright.daemon import Daemon, Scope
from tapwright.server import create_app
from fakes import Clock, FakeSession, button, el
from test_daemon import finish, make, run, started

PHONE = (400.0, 800.0)


def kinds(result) -> list[str]:
    return [f.kind for f in result.findings]


def wide(ref, label, y, width=300, height=48):
    return button(ref, label, rect=(50, y, width, height))


@pytest.fixture(autouse=True)
def clean_memory():
    uxcheck._SEEN.clear()
    yield
    uxcheck._SEEN.clear()


# ---- the rules ----
def test_rules_are_text_or_measured_keys():
    rules = uxcheck.parse({"version": 1, "rules": ["core features within 2 taps of home", {"max_taps_to_goal": 4},
                                                   {"way_back": True}]})
    assert rules.texts == ("core features within 2 taps of home",) and rules.max_taps_to_goal == 4 and rules.way_back
    assert uxcheck.loads(rules.dumps()) == rules
    assert rules.listing() == ["core features within 2 taps of home", "max_taps_to_goal: 4",
                               "way_back: every screen has a visible way back (a back button, a close control or a tab bar)"]
    assert not uxcheck.NONE and uxcheck.parse({}) == uxcheck.NONE


@pytest.mark.parametrize("bad,message", [
    ([], "must be a JSON object"),
    ({"rule": []}, "unknown key 'rule'"),
    ({"rules": [{"max_taps": 3}]}, "unknown key 'max_taps' in a rule"),
    ({"rules": [{"way_back": True, "max_taps_to_goal": 2}]}, "exactly one key"),
    ({"rules": [{"max_taps_to_goal": 0}]}, "whole number of 1 or more"),
    ({"rules": [{"max_taps_to_goal": True}]}, "whole number of 1 or more"),
    ({"rules": [{"way_back": "yes"}]}, "true or false"),
    ({"rules": ["  "]}, "must not be empty"),
    ({"rules": [7]}, "text or an object"),
    ({"rules": "x"}, "list of rules"),
    ({"version": 2}, "version must be 1"),
])
def test_a_bad_rule_is_refused_saying_what_is_wrong(bad, message):
    with pytest.raises(ValueError, match=message):
        uxcheck.parse(bad)


def test_a_bad_rules_file_is_refused_naming_the_file_and_mcp_will_not_start(tmp_path):
    path = tmp_path / "ux.json"
    path.write_text('{"rules": [{"nope": 1}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="ux.json"):
        uxcheck.load(path)
    with pytest.raises(ValueError, match="can't read the ux rules file"):
        uxcheck.load(tmp_path / "missing.json")
    done = subprocess.run([sys.executable, "-m", "tapwright", "mcp", "--ux-rules", str(path)], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode != 0 and "unknown key 'nope'" in done.stderr
    path.write_text('{"rules": ["a", {"way_back": true}]}', encoding="utf-8")
    assert uxcheck.load(path).way_back


def test_the_rules_go_in_a_header_and_the_server_reads_it_or_refuses_it():
    client.SCOPE.clear()
    client.set_ux_rules(None)
    assert "X-Tapwright-Ux-Rules" not in client.SCOPE
    client.set_ux_rules(uxcheck.NONE)
    assert "X-Tapwright-Ux-Rules" not in client.SCOPE
    rules = uxcheck.parse({"rules": ["x", {"way_back": True}]})
    client.set_ux_rules(rules)
    assert uxcheck.loads(client.SCOPE["X-Tapwright-Ux-Rules"]) == rules
    client.SCOPE.clear()

    token = "t" * 43
    auth = {"Authorization": f"Bearer {token}", "X-Tapwright-Client": "agent-1", "X-Tapwright-Mode": "dedicated"}
    clock = Clock()
    d = Daemon(FakeSession(), control=Controller(clock=clock, sleep=clock.sleep), log=lambda *a: None,
               stream_linger=0, stream_retry=0.01)
    d._set_state("live")
    d.session.tree = [button("e1", "Save", "save", rect=(10, 100, 100, 40))]
    with TestClient(create_app(d, token, hosts={"testserver"})) as c:
        r = c.post("/api/ux_check", headers={**auth, "X-Tapwright-Ux-Rules": rules.dumps()}, json={})
        assert r.status_code == 200 and "way_back" in r.json()["text"] and '"x"' in r.json()["text"]
        for bad in ("{not json", '{"rules": [{"zzz": 1}]}', "[]"):
            r = c.post("/api/ux_check", headers={**auth, "X-Tapwright-Ux-Rules": bad}, json={})
            assert r.status_code == 400 and r.json()["error"]["code"] == "bad_params"


# ---- the measures ----
def test_many_competing_choices_are_counted_without_list_rows():
    items = [button(f"e{n}", f"Item {n}", rect=(10, 60 + n * 50, 90, 40)) for n in range(1, 13)]
    items += [el(f"e{20 + n}", "cell", f"Row {n}", rect=(0, 700, 400, 40)) for n in range(3)]
    result = uxcheck.check(items, PHONE)
    assert result.choices == 12 and result.rows == 3
    assert kinds(result) == ["choices"]
    assert result.findings[0].what == "12 actionable elements compete for attention"
    assert result.findings[0].basis.startswith("best practice: ")
    few = uxcheck.check(items[:5], PHONE)
    assert "choices" not in kinds(few)


def test_one_clear_main_action_or_two_alike():
    clear = [wide("e1", "Continue", 700, 320, 56), wide("e2", "Skip", 640, 120, 40)]
    assert uxcheck.check(clear, PHONE).main.ref == "e1" and "main_action" not in kinds(uxcheck.check(clear, PHONE))
    alike = [wide("e1", "Accept", 700), wide("e2", "Decline", 640)]
    result = uxcheck.check(alike, PHONE)
    assert kinds(result) == ["main_action"]
    assert 'no one main action: "Accept" and "Decline" are about the same size' in result.findings[0].what
    assert result.findings[0].basis == "measured"
    lone = [wide("e1", "Save", 700)]
    assert kinds(uxcheck.check(lone, PHONE)) == []
    small = [button("e1", "Back", rect=(10, 60, 60, 30)), button("e2", "Edit", rect=(330, 60, 60, 30))]
    assert uxcheck.check(small, PHONE).main is None  # nav bar buttons are not main actions


def test_a_main_action_in_the_top_third_is_out_of_thumb_reach_on_a_phone_only():
    high = [wide("e1", "Save", 100, 320, 56)]
    result = uxcheck.check(high, PHONE)
    assert kinds(result) == ["reach"]
    assert "top third" in result.findings[0].what and result.findings[0].basis.startswith("best practice: ")
    assert kinds(uxcheck.check([wide("e1", "Save", 400, 320, 56)], PHONE)) == []  # the middle is fine
    assert kinds(uxcheck.check(high, (1024.0, 1366.0))) == []  # a tablet
    assert kinds(uxcheck.check(high, (800.0, 400.0))) == []  # landscape


def test_an_icon_only_button_without_a_label_is_a_finding_and_a_labelled_one_is_not():
    items = [button("e1", "", "menu", rect=(10, 60, 44, 44)), button("e2", "Settings", rect=(300, 60, 44, 44))]
    result = uxcheck.check(items, PHONE)
    assert kinds(result) == ["icon_only"] and result.findings[0].element.startswith("e1 button id=menu")


def test_audit_rows_become_cut_off_text_contrast_and_target_size():
    rows = [("clipped_text", 'e3 text "Total"', "its text is cut off"), ("contrast", 'e4 text "Hint"', "hard to read"),
            ("hit_region", 'e5 button "x"', "too small to tap reliably"), ("trait", "e6 text", "traits")]
    result = uxcheck.check([], PHONE, audit_rows=rows)
    assert kinds(result) == ["cut_off_text", "contrast", "target_size"]
    assert {f.basis for f in result.findings} == {"measured"}


def test_an_error_state_with_no_next_step_near_it_is_a_dead_end():
    lost = [el("e1", "text", "No results found", rect=(20, 300, 360, 24)),
            button("e2", "Settings", rect=(10, 700, 100, 40))]
    result = uxcheck.check(lost, PHONE)
    assert kinds(result) == ["dead_end"] and result.findings[0].element.startswith('e1 text "No results found"')
    helped = [lost[0], button("e3", "Clear filters", rect=(20, 340, 160, 44))]
    assert kinds(uxcheck.check(helped, PHONE)) == []
    assert kinds(uxcheck.check([el("e1", "text", "Welcome back", rect=(20, 300, 360, 24))], PHONE)) == []


def test_the_same_action_named_differently_on_an_earlier_screen_is_naming_drift():
    first = [el("e1", "header", "Welcome", rect=(0, 60, 400, 30)), button("e2", "Log in", rect=(10, 600, 100, 40))]
    second = [el("e1", "header", "Account", rect=(0, 60, 400, 30)), button("e2", "Sign in", rect=(10, 600, 100, 40))]
    assert kinds(uxcheck.check(first, PHONE, "agent")) == []
    result = uxcheck.check(second, PHONE, "agent")
    assert kinds(result) == ["naming"]
    assert result.findings[0].what == 'this action is "Sign in" here but "Log in" on "Welcome"'
    # another agent has seen nothing, and the same screen again is not drift against itself
    assert kinds(uxcheck.check(second, PHONE, "other")) == []
    assert kinds(uxcheck.check(second, PHONE, "agent")) == ["naming"]
    both = [button("e1", "Save", rect=(10, 600, 100, 40)), button("e2", "Done", rect=(150, 600, 100, 40))]
    assert "naming" not in kinds(uxcheck.check(both, PHONE, "third"))  # two names on one screen is not drift
    assert [s.title for s in uxcheck.screens_seen("agent")] == ["Welcome", "Account"]


def test_way_back_is_checked_on_the_current_screen_when_the_rule_is_on():
    rules = uxcheck.parse({"rules": [{"way_back": True}]})
    bare = [el("e1", "text", "Settings", rect=(20, 300, 200, 24)), button("e2", "Save", rect=(10, 700, 100, 40))]
    result = uxcheck.check(bare, PHONE, rules=rules)
    assert kinds(result) == ["way_back"] and result.findings[0].basis == "house rule: way_back"
    assert kinds(uxcheck.check(bare, PHONE)) == []  # rule off: not checked
    for ok in ([button("e2", "Back", rect=(10, 60, 60, 30))], [button("e2", "Close", rect=(340, 60, 50, 30))],
               [button("e2", "Trips", rect=(10, 40, 60, 30))], [el("e2", "tab", "Home", rect=(0, 740, 100, 50))]):
        assert kinds(uxcheck.check(ok, PHONE, rules=rules)) == []


def test_the_report_has_the_format_of_the_spec():
    items = [button("e1", "", "menu", rect=(10, 60, 44, 44)), wide("e2", "Accept", 700), wide("e3", "Decline", 640)]
    rules = uxcheck.parse({"rules": ["core features within 2 taps of home", {"max_taps_to_goal": 4}]})
    text = uxcheck.report('"Wayfare"', uxcheck.check(items, PHONE, rules=rules), rules)
    lines = text.splitlines()
    assert lines[0] == 'UX check of "Wayfare": 2 findings.'
    assert lines[1].startswith('1. main_action: e2 button "Accept" ') and lines[1].endswith(" (measured)")
    assert lines[2].startswith("2. icon_only: e1 button id=menu @10,60 44x44: ") and lines[2].endswith("(measured)")
    assert lines[3].startswith("Measured: 3 actionable elements; main action: e2 button \"Accept\"")
    assert lines[4] == ('House rules to judge from the screen (they are yours to decide, say so): '
                        '"core features within 2 taps of home".')
    one = uxcheck.report('"A"', uxcheck.check([button("e1", "", "m", rect=(1, 1, 44, 44))], PHONE))
    assert one.startswith('UX check of "A": 1 finding.\n')
    none = uxcheck.report('"A"', uxcheck.check([], PHONE))
    assert none.splitlines()[0] == 'UX check of "A": 0 findings.'
    assert "main action: none found" in none


# ---- the daemon ----
def test_the_ux_check_tool_reads_the_screen_runs_the_audit_and_answers_in_the_format():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [el("e1", "header", "Settings", rect=(0, 60, 400, 30)),
                          button("e2", "", "gear", rect=(340, 60, 44, 44)),
                          el("e3", "text", "Total price", rect=(16, 500, 200, 18))]
        d.session.audit_issues = [{"auditType": audit.XCTEST["clipped_text"], "compactDescription": "Text clipped",
                                   "elementAttributes": {"type": "XCUIElementTypeStaticText", "label": "Total price",
                                                         "name": "", "rect": {"x": 16, "y": 500, "width": 200, "height": 18}}}]
        r = await d.run_tool("a", "ux_check", {})
        lines = r["text"].splitlines()
        assert lines[0] == 'UX check of "Example" (Settings): 2 findings.'
        assert any(ln.startswith("1. icon_only: e2 button id=gear") for ln in lines)
        assert any(ln.startswith('2. cut_off_text: e3 text "Total price"') and ln.endswith("Text clipped (measured)")
                   for ln in lines)
        assert ("audit", tuple(audit.XCTEST[t] for t in ("clipped_text", "contrast", "hit_region"))) in d.session.calls
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "ux_check", {"types": ["contrast"]})
        assert e.value.code == "bad_params" and str(e.value) == "ux_check doesn't take types; it takes no arguments."
        await finish(stop, task)
    run(go)


def test_the_house_rules_of_the_call_reach_the_check_and_a_look_remembers_the_screen_for_naming():
    async def go():
        d = make()
        stop, task = await started(d)
        rules = uxcheck.parse({"rules": ["keep it simple", {"way_back": True}]})
        d.session.tree = [el("e1", "header", "Welcome", rect=(0, 60, 400, 30)), button("e2", "Log in", rect=(10, 600, 100, 40))]
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.tree = [el("e1", "header", "Account", rect=(0, 60, 400, 30)), button("e2", "Sign in", rect=(10, 600, 100, 40))]
        r = await d.run_tool("a", "ux_check", {}, ux_rules=rules)
        assert 'naming: ' in r["text"] and 'button "Sign in"' in r["text"] and "this action is" in r["text"]
        assert "way_back: the screen: no visible back button" in r["text"] and "(house rule: way_back)" in r["text"]
        assert '"keep it simple"' in r["text"]
        assert uxcheck.rules_in_force() == uxcheck.NONE  # the rules belong to the call
        again = await d.run_tool("a", "ux_check", {})
        assert "way_back" not in again["text"] and "House rules" not in again["text"]
        await finish(stop, task)
    run(go)


def test_ux_check_is_a_read_in_personal_mode_and_refused_outside_the_given_apps():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [button("e1", "", "gear", rect=(340, 60, 44, 44))]
        d.session.foreground = "com.example.app"
        ok = await d.run_tool("a", "ux_check", {}, scope=Scope(("com.example.app",)))
        assert ok["text"].startswith('UX check of "Example": 1 finding.')
        d.session.foreground = "com.apple.mobilesafari"
        d.session.app = ("Safari", "com.apple.mobilesafari")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "ux_check", {}, scope=Scope(("com.example.app",)))
        assert e.value.code == "not_allowed"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "ux_check", {}, scope=Scope(()))
        assert e.value.code == "not_allowed"
        await finish(stop, task)
    run(go)


def test_findings_are_added_to_a_running_journey_when_there_is_one(monkeypatch):
    from tapwright import journey as journey_module
    got = []
    monkeypatch.setattr(journey_module, "add_finding", lambda client_id, finding: got.append((client_id, finding)))

    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [button("e1", "", "gear", rect=(340, 60, 44, 44))]
        await d.run_tool("agent-7", "ux_check", {})
        await finish(stop, task)
    run(go)
    assert len(got) == 1 and got[0][0] == "agent-7"
    assert got[0][1]["kind"] == "icon_only" and got[0][1]["basis"] == "measured" and got[0][1]["screen"] == '"Example"'


def test_a_phone_without_the_audit_still_gets_the_other_facts():
    async def go():
        d = make()
        stop, task = await started(d)
        from tapwright.device import DeviceError
        d.session.tree = [button("e1", "", "gear", rect=(340, 60, 44, 44))]
        d.session.fail[("audit", ("XCUIAccessibilityAuditTypeTextClipped", "XCUIAccessibilityAuditTypeContrast",
                                   "XCUIAccessibilityAuditTypeHitRegion"))] = DeviceError("HTTP 404")
        r = await d.run_tool("a", "ux_check", {})
        assert "1. icon_only" in r["text"] and "audit isn't available here" in r["text"]
        await finish(stop, task)
    run(go)


def test_cr_enabled_sliders_are_counted_as_choices():
    items = [el("e1", "slider", "Volume", rect=(20, 100, 300, 40)), el("e2", "slider", "Off", rect=(20, 200, 300, 40),
                                                                           enabled=False),
             button("e3", "Save", rect=(20, 300, 100, 40))]
    assert uxcheck.check(items, PHONE).choices == 2


def test_cr_a_symbolic_close_label_is_a_way_back():
    rules = uxcheck.parse({"rules": [{"way_back": True}]})
    for symbol in ("×", "✕", "‹", "<", "x"):
        only = [el("e1", "text", "Settings", rect=(20, 300, 200, 24)), button("e2", symbol, rect=(340, 60, 40, 30))]
        assert kinds(uxcheck.check(only, PHONE, rules=rules)) == [], symbol
