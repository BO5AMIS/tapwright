"""One-time codes (docs/AGENT-TOOLS.md §20.4, §24.3) and the `/otp` route over the link (§18.11): the code store hands a
code over once within its 2 minutes, and the route answers only with Tapwright's token, on a connection that just proved
the daemon, for an app whose setting is on; `404` otherwise. Synthetic values only."""
import json

from fakes import Clock, FakeSession
from starlette.testclient import TestClient

from tapwright import otp
from tapwright.control import Controller
from tapwright.daemon import Daemon
from tapwright.server import COOKIE, create_app

TOKEN = "t0ken-for-tests"
APP = "com.example.wayfare"
A, B = ("127.0.0.1", 50001), ("127.0.0.1", 50002)  # two connections: another port is another connection


def test_a_code_is_handed_over_once_within_its_two_minutes():
    now = [1000.0]
    kept = []
    codes = otp.Codes(kept.append, clock=lambda: now[0])
    codes.put(APP, "834201", "com.apple.MobileSMS", field_shown_at=1000.0)
    assert kept == ["834201"]  # kept out of text from the moment it is known
    got = codes.take(APP)
    assert (got.code, got.source, got.expires) == ("834201", "com.apple.MobileSMS", 1120.0)
    assert "834201" not in repr(got)
    assert codes.take(APP) is None  # once
    codes.put(APP, "834202", "com.apple.MobileSMS", field_shown_at=1000.0)
    now[0] = 1120.5
    assert codes.take(APP) is None  # past the 2 minutes
    assert otp.answer(otp.Code("834203", "com.apple.mobilemail", 1.5)) == {
        "code": "834203", "expires": 1500, "source": "com.apple.mobilemail"}


def test_a_proved_connection_may_ask_for_a_short_while_and_no_other():
    now = [0.0]
    proved = otp.Proved(clock=lambda: now[0])
    proved.mark(A)
    assert proved.holds(A) and not proved.holds(B) and not proved.holds(None)
    now[0] = otp.PROVED_FOR + 1
    assert not proved.holds(A)
    for port in range(otp.MAX_PROVED + 5):
        proved.mark(("127.0.0.1", port))
    assert len(proved._at) <= otp.MAX_PROVED


def daemon_with(tmp_path, setting=True):
    path = tmp_path / "vault.json"
    data = {"version": 2, "vault": {"n": {"kind": "note", "text": "synthetic"}}}
    if setting:
        data["otp"] = {APP: {"from": ["com.apple.MobileSMS"], "match": "Wayfare"}}
    path.write_text(json.dumps(data), "utf-8")
    clock = Clock()
    session = FakeSession()
    # the app in front with its code field: /otp hands a code over only while the screen shows both (M3)
    from tapwright.ui import Element
    session.tree = [Element("e1", "input", "Verification code", "otp-input", type="TextField")]
    session.tree_app = ("Wayfare", APP)
    d = Daemon(session, control=Controller(clock=clock, sleep=clock.sleep), log=lambda *a: None,
               stream_linger=0, stream_retry=0.01)
    d._set_state("live")
    from tapwright.ceiling import Ceiling
    d.ceiling = Ceiling(dedicated=True, accounts=str(path))  # (/otp needs serve's scope: Codex, PR #12 cycle 2, M5)
    return create_app(d, TOKEN, hosts={"testserver"}, ceiling=d.ceiling), d


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def prove(c):
    got = c.post("/api/prove", json={"challenge": "ab" * 32})
    assert got.status_code == 200 and TOKEN not in got.text


def test_the_route_needs_the_token_and_a_proof_on_the_same_connection(tmp_path):
    app, d = daemon_with(tmp_path)
    a, b = TestClient(app, client=A), TestClient(app, client=B)
    assert a.post("/otp", json={"app": APP}).status_code == 401  # no token
    assert a.post("/otp", json={"app": APP}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    a.cookies.set(COOKIE, TOKEN)
    assert a.post("/otp", json={"app": APP}).status_code == 401  # the cookie is never enough
    a.cookies.clear()
    unproved = a.post("/otp", json={"app": APP}, headers=AUTH)
    assert unproved.status_code == 403 and "prove this daemon first" in unproved.text
    prove(a)
    assert b.post("/otp", json={"app": APP}, headers=AUTH).status_code == 403  # another connection: no
    none = a.post("/otp", json={"app": APP}, headers=AUTH)
    assert none.status_code == 404 and none.json()["error"]["code"] == "not_found"  # no notifications read yet
    assert a.post("/otp", json={"nope": 1}, headers=AUTH).status_code == 400
    assert a.post("/otp", content=b"{bad", headers=AUTH).status_code == 400
    assert a.post("/otp", json={"app": APP}, headers={**AUTH, "host": "evil.example"}).status_code == 403
    assert a.get("/otp", headers=AUTH).status_code == 405


def test_a_code_goes_over_once_with_its_source_and_only_for_an_app_whose_setting_is_on(tmp_path):
    app, d = daemon_with(tmp_path)
    c = TestClient(app, client=A)
    prove(c)
    import time
    d.otp_codes.put(APP, "834201", "com.apple.MobileSMS", time.time())
    got = c.post("/otp", json={"app": APP}, headers=AUTH)
    assert got.status_code == 200 and set(got.json()) == {"code", "expires", "source"}
    assert got.json()["code"] == "834201" and got.json()["source"] == "com.apple.MobileSMS"
    assert got.headers["cache-control"] == "no-store"
    assert c.post("/otp", json={"app": APP}, headers=AUTH).status_code == 404  # handed over once
    d.otp_codes.put("com.other.app", "834202", "com.apple.MobileSMS", time.time())
    assert c.post("/otp", json={"app": "com.other.app"}, headers=AUTH).status_code == 404  # its setting is off
    assert d.scrub_everywhere("code 834201") == "code <one-time code>"


def test_without_the_setting_no_code_goes_over(tmp_path):
    app, d = daemon_with(tmp_path, setting=False)
    c = TestClient(app, client=A)
    prove(c)
    import time
    d.otp_codes.put(APP, "834201", "com.apple.MobileSMS", time.time())
    assert c.post("/otp", json={"app": APP}, headers=AUTH).status_code == 404
