"""CodeRabbit's findings on PR #9 that reach the driving layer (trip.py): the read-only replay, the trip's own app,
a sheet over the page, the cleaned names of a repair; and a tree whose root has no frame (ui.py)."""
import pytest
from mapfakes import MapSession, screen
from test_daemon import finish, run, started
from test_daemon_drive import BUNDLE, drive, learn, mapped

from tapwright import accounts, ui
from tapwright.control import Refused
from tapwright.daemon import SCRIPT


def failed_of(d, ident):
    return [r["failed"] for r in d.maps.data(None, BUNDLE)["roads"] if r["move"]["target"].get("id") == ident]


def test_a_replay_that_cannot_find_its_target_never_counts_a_failure_against_the_saved_road(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        d.session.screens["settings"] = screen("Settings", ids=("about-row", "s1", "s2", "s3"))
        token = SCRIPT.set({"path": tmp_path / "s.json", "update": False})
        try:
            for _ in range(3):  # a drive's `FAILS` is 2: it would have dropped the road
                with pytest.raises(Refused) as e:
                    await drive(d, to="language")
                assert e.value.code == "off_route"
        finally:
            SCRIPT.reset(token)
        assert failed_of(d, "general-row") == [0]
        await finish(stop, task)
    run(go)


class Switching(MapSession):
    """After a move the phone shows the same screen, but of another app."""
    other = False

    def _go(self, key) -> None:
        super()._go(key)
        if self.other and self.at == "settings":
            self.app = ("Other", "com.other.app")


def test_another_app_with_the_same_screen_is_not_an_arrival_in_the_trips_app(tmp_path):
    async def go():
        d = mapped(tmp_path)
        d.session.__class__ = Switching
        stop, task = await started(d)
        await learn(d)
        d.session.at = "home"
        d.session.other = True
        with pytest.raises(Refused) as e:
            await drive(d, to="settings")
        assert e.value.code == "off_route" and "doesn't know" in str(e.value)
        await finish(stop, task)
    run(go)


def test_a_covering_screen_the_map_doesnt_know_still_gets_its_dismissing_reflex_while_waiting(tmp_path):
    async def go():
        cover = screen("Cover", ids=("not-now", "c1", "c2", "c3"), texts=("Rate Wayfare",))
        d = mapped(tmp_path, extra={"cover": cover})
        stop, task = await started(d)
        await learn(d)
        d.session.goes.update({("home", "settings-btn"): "cover", ("cover", "not-now"): "settings"})
        d.session.at = "cover"
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "map", {"reflex": {"name": "rate", "when": {"text": "Rate Wayfare"},
                                                 "do": {"tool": "tap", "target": {"id": "not-now"}}}})
        d.session.at = "home"
        await d.run_tool("a", "look", {"screenshot": False})
        r = await drive(d, to="settings")
        assert r["text"].startswith("Arrived at settings") and "Reflex: closed" in r["text"]
        await finish(stop, task)
    run(go)


def test_a_repaired_roads_names_never_hold_an_accounts_value(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await learn(d)
        d.session.screens["settings"] = screen("Settings", ids=("general-row2", "about-row", "s1", "s2", "s3"))
        d.session.goes[("settings", "general-row2")] = "general"
        known = {"t": accounts.Account("t", "Label of general-row2", "pw-secret-1")}
        d._loaded = lambda: accounts.Loaded("x", accounts=known)
        await drive(d, to="language")
        repaired = [r for r in d.maps.data(None, BUNDLE)["roads"] if r["move"]["target"] == {"id": "general-row2"}]
        assert repaired and "label" not in repaired[0]["names"] and repaired[0]["names"]["id"] == "general-row2"
        await finish(stop, task)
    run(go)


def test_a_root_with_no_frame_and_a_tall_wrapper_still_give_the_screen_its_structure():
    def n(kind, x, y, w, h, name="", kids=()):
        return (f'<XCUIElementType{kind} type="XCUIElementType{kind}" name="{name}" label="" enabled="true" '
                f'x="{x}" y="{y}" width="{w}" height="{h}">{"".join(kids)}</XCUIElementType{kind}>')
    # the wrapper's centre (y 1400) is far below the 800 point screen; its first rows are in view
    wrapper = n("ScrollView", 0, 100, 400, 2600, kids=[n("TextField", 10, 120, 300, 40, "email")])
    xml = f"<AppiumAUT>{n('Application', 0, 0, 400, 800, kids=[wrapper])}</AppiumAUT>"
    tree = ui.read(xml, (400.0, 800.0))
    assert tree.structure.kinds == ("form",) and tree.structure.ids == ("email",)
    bare = "<AppiumAUT><XCUIElementTypeApplication type=\"XCUIElementTypeApplication\">" \
           + n("Table", 0, 100, 400, 500) + "</XCUIElementTypeApplication></AppiumAUT>"
    assert ui.read(bare, (400.0, 800.0)).structure.kinds == ("list",)


def test_drive_waits_as_long_as_a_run_for_the_daemons_answer():
    from tapwright import mcp_server
    assert mcp_server._timeout("drive", {"to": "x", "then": []}) == mcp_server.LONG_TIMEOUT
    assert mcp_server._timeout("tap", {}) == mcp_server.TIMEOUT


def test_a_header_outside_latin_1_is_sent_percent_encoded_and_read_back_by_the_daemon():
    from types import SimpleNamespace

    from tapwright import server, thin

    def given(apps):
        return SimpleNamespace(apps=apps, client="c", dedicated=False, policy=None, ux_rules=None, maps=None, accounts=None)
    wechat = "微信,com.a.b"
    sent = thin._headers(given(wechat), "tok")
    assert all(v.isascii() for v in sent.values()) and sent["X-Tapwright-Encoding"] == "percent"
    request = SimpleNamespace(headers={k.lower(): v for k, v in sent.items()})
    assert server._hv(request, "x-tapwright-apps") == wechat
    plain = thin._headers(given("com.a.b"), "tok")
    assert "X-Tapwright-Encoding" not in plain and plain["X-Tapwright-Apps"] == "com.a.b"  # old callers are unchanged


def test_a_literal_account_placeholder_cannot_be_written_in_a_route():
    from tapwright import routes
    route = {"name": "r", "start": "home", "variables": [], "steps": [{"tool": "type_text", "target": {"id": "f"},
                                                                       "text": "$${account.password}"}]}
    with pytest.raises(Refused) as e:
        routes.check(route)
    assert e.value.code == "bad_params" and "literal account placeholder" in str(e.value)
