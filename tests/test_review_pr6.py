"""Fixes from Codex's review of PR #6 (1.4.0): secrets in recordings, network owners, policy on the live front app,
launch overrides, indexed reach, the hold's own death."""
import asyncio
import json

import pytest
from fakes import button, el
from test_daemon import LongList, finish, make, run, started

from tapwright import daemon as daemon_module, policy
from tapwright.control import Refused


# ---- P1: a secure field below the screen is never recorded in plaintext ----
def test_recording_never_saves_text_typed_into_a_secure_field_that_was_off_screen(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.screen = (400.0, 800.0)
        s.tree = [el("e1", "scroll", "", "form", rect=(0, 0, 400, 800)), el("e2", "input", "Email", "email",
                                                                                 rect=(10, 100, 300, 44))]
        s.offscreen = [el("", "input", "Password", "pw", rect=(10, 1000, 300, 44), secure=True)]
        real = s.scroll

        async def scroll(x0, y0, x1, y1):  # the form moves: the password field comes up
            s.tree = [s.tree[0], el("e2", "input", "Password", "pw", rect=(10, 300, 300, 44), secure=True)]
            s.offscreen = []
            return await real(x0, y0, x1, y1)
        s.scroll = scroll
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "name": "sign in"})
        await d.run_tool("a", "type_text", {"target": {"id": "pw"}, "text": "hunter2", "look": False})
        assert s.typed and s.typed[-1][0] == "hunter2"  # it was typed, after the scroll
        path = tmp_path / "s.json"
        r = await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        saved = path.read_text("utf-8")
        assert "hunter2" not in saved and "hunter2" not in r["text"]
        assert json.loads(saved)["steps"] == [{"tool": "type_text", "text": "", "secure": True,
                                               "target": {"id": "pw"}}]
        assert "Step 1 typed into a secure field" in r["text"]
        await finish(stop, task)
    run(go)


# ---- P1: `network` checks the policy against the app in front now ----
def test_network_checks_the_per_app_policy_against_the_app_in_front_now():
    async def go():
        d = make()
        stop, task = await started(d)
        p = policy.parse({"apps": {"com.blocked.app": {"block": ["network"]}}})
        await d.run_tool("a", "look", {"screenshot": False})  # the last read: com.example.app
        d.session.foreground = "com.blocked.app"  # a person put the blocked app in front since
        d.session.app_names["com.blocked.app"] = "Blocked"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "network", {"mode": "offline"}, policy=p)
        assert e.value.code == "not_allowed" and "blocks network for" in str(e.value)
        assert d.session.device.network_calls == [] and d._network is None
        d.session.foreground = "com.example.app"
        assert (await d.run_tool("a", "network", {"mode": "3g"}, policy=p))["text"].startswith("Network: 3g")
        await finish(stop, task)
    run(go)


def test_network_fails_closed_when_the_front_app_cannot_be_read_and_a_policy_blocks_it_per_app():
    async def go():
        d = make()
        stop, task = await started(d)
        p = policy.parse({"apps": {"com.blocked.app": {"block": ["network"]}}})

        async def broken():
            raise RuntimeError("no answer")
        d.session.foreground_bundle_id = broken
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "network", {"mode": "3g"}, policy=p)
        assert e.value.code == "not_allowed" and d.session.device.network_calls == []
        # no per-app word anywhere: nothing depends on the front app, so it still works
        assert (await d.run_tool("a", "network", {"mode": "3g"}))["text"].startswith("Network: 3g")
        await finish(stop, task)
    run(go)


# ---- P2: a plain open_app after one with overrides starts the app again with the defaults ----
def test_a_plain_open_app_after_launch_overrides_relaunches_with_the_defaults():
    async def go():
        d = make()
        stop, task = await started(d)
        app = {"app": "com.example.app", "look": False}
        await d.run_tool("a", "open_app", {**app, "language": "ar", "text_size": "ax5"})
        d.session.closed.clear()
        r = await d.run_tool("a", "open_app", app)
        assert d.session.closed == ["com.example.app"]  # the running app is closed: WebDriverAgent would bring it forward
        assert d.session.launch_args[-1] == []
        assert r["text"] == "Opened Example (com.example.app), back to the phone's own language and text size."
        d.session.closed.clear()
        await d.run_tool("a", "open_app", app)  # defaults already: left alone, as before
        assert d.session.closed == []
        await d.run_tool("a", "open_app", {**app, "region": "QA"})
        d.session.closed.clear()
        await d.run_tool("a", "open_app", {**app, "text_size": "system"})  # `system` is the phone's own setting
        assert d.session.closed == ["com.example.app"] and d.session.launch_args[-1] == []
        await d.run_tool("a", "open_app", {**app, "language": "ar"})
        await d.run_tool("a", "close_app", {"app": "com.example.app"})  # closing forgets the overrides
        d.session.closed.clear()
        await d.run_tool("a", "open_app", app)
        assert d.session.closed == []
        await finish(stop, task)
    run(go)


# ---- P2: an indexed target that is off screen is acted on after the scroll brings it on ----
def test_a_tap_on_an_indexed_off_screen_match_taps_that_element_after_the_scroll():
    async def go():
        d = make()
        stop, task = await started(d)
        rows = LongList(d, rows=40, ids=True, labels=lambda i: "Account")
        await d.run_tool("a", "look", {"screenshot": False})
        r = await d.run_tool("a", "tap", {"target": {"label": "Account", "index": 30}, "look": False})
        assert r["text"].startswith("Scrolled to it, then tapped") or "Tapped" in r["text"], r["text"]
        x, y = d.session.tap_points[-1]
        assert y == 30 * rows.pitch + rows.offset + rows.height / 2  # row 30's centre, not another Account's
        await finish(stop, task)
    run(go)


def test_type_text_into_an_indexed_off_screen_input_types_into_that_input():
    async def go():
        d = make()
        stop, task = await started(d)
        s = d.session
        s.tree = [el("e1", "scroll", "", "form", rect=(0, 0, 400, 800)),
                  el("e2", "input", "Note", "n0", rect=(10, 100, 300, 44))]
        s.offscreen = [el("", "input", "Note", "n1", rect=(10, 1000, 300, 44)),
                       el("", "input", "Note", "n2", rect=(10, 1500, 300, 44))]
        real = s.scroll

        async def scroll(x0, y0, x1, y1):  # the form moves up by 700: only the second Note is on screen now
            s.tree = [s.tree[0], el("e2", "input", "Note", "n1", rect=(10, 300, 300, 44))]
            s.offscreen = [el("", "input", "Note", "n0", rect=(10, -600, 300, 44)),
                           el("", "input", "Note", "n2", rect=(10, 800, 300, 44))]
            return await real(x0, y0, x1, y1)
        s.scroll = scroll
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "type_text", {"target": {"label": "Note", "index": 1}, "text": "hi", "look": False})
        assert s.typed and s.typed[-1][0] == "hi"
        assert next(e for e in s.items if e.ref == s.typed[-1][3].ref).y == 300  # the one that came on screen
        await finish(stop, task)
    run(go)


# ---- P2: a condition connection that closes while the hold waits ends the condition ----
def _fake_instruments(monkeypatch, dtx):
    """pymobiledevice3's DVT modules, as far as NetworkHold uses them: one provider whose transport is `dtx`."""
    import sys
    import types

    class Provider:
        def __init__(self, rsd):
            self._dtx = dtx

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Inducer:
        cleared = 0

        def __init__(self, provider):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def list(self):
            return [{"profiles": [{"identifier": "SlowNetwork3GGood", "description": "3G Good"}]}]

        async def set(self, profile):
            pass

        async def clear(self):
            Inducer.cleared += 1

    for name in ("pymobiledevice3", "pymobiledevice3.services", "pymobiledevice3.services.dvt",
                 "pymobiledevice3.services.dvt.instruments"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    inducer, provider = (types.ModuleType(n) for n in ("condition_inducer", "dvt_provider"))
    inducer.ConditionInducer, provider.DvtProvider = Inducer, Provider
    monkeypatch.setitem(sys.modules, "pymobiledevice3.services.dvt.instruments.condition_inducer", inducer)
    monkeypatch.setitem(sys.modules, "pymobiledevice3.services.dvt.instruments.dvt_provider", provider)
    return Inducer


def test_a_closed_condition_connection_is_noticed_while_the_hold_waits(monkeypatch):
    from tapwright.device import Device, NetworkHold

    class Dtx:
        def __init__(self):
            self.gone = asyncio.Event()

        async def wait_disconnected(self):
            await self.gone.wait()

    async def go():
        dtx = Dtx()
        inducer = _fake_instruments(monkeypatch, dtx)
        dev = Device(log=lambda *a: None)
        dev.rsd = "tunnel"
        told: list[str] = []
        dev.on_network_end = told.append
        hold = NetworkHold(dev, "3g")
        dev._network_hold = hold
        dev._network_holds.add(hold)
        assert await hold.start() == "SlowNetwork3GGood"
        assert told == []
        dtx.gone.set()  # only the condition's connection closes; the tunnel and WebDriverAgent live on
        for _ in range(50):
            if told:
                break
            await asyncio.sleep(0.01)
        assert len(told) == 1 and "the phone's condition connection closed" in told[0]
        assert dev._network_hold is None and hold not in dev._network_holds
        # an ordinary stop is not a death: it clears the condition and tells nobody
        again = NetworkHold(dev, "3g")
        dev._network_hold = again
        dev._network_holds.add(again)
        await again.start()
        await dev.clear_network()
        assert len(told) == 1 and inducer.cleared == 1
    run(go)


# ---- P2: a failing batch clears only the condition it set ----
def test_a_failing_batch_does_not_clear_another_agents_replacement_condition():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [button("e1", "Save", "save")]

        real_sleep, swapped = d.control.sleep, []

        async def sleep(seconds):  # while A's script waits, B sets its own condition (once)
            if not swapped:
                swapped.append(1)
                await d.run_tool("b", "network", {"mode": "lossy"})
            return await real_sleep(seconds)
        d.control.sleep = sleep
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "network", "mode": "3g"}, {"tool": "wait", "seconds": 0.1},
                                                    {"tool": "tap", "target": {"id": "missing"}}]})
        assert "stopped at step 3" in str(e.value)
        assert "the network condition this script set was cleared" not in str(e.value)  # it wasn't A's any more
        assert d._network == "lossy" and d._network_by == "b"
        assert d.session.device.network_calls == [("set", "3g"), ("set", "lossy")]
        await finish(stop, task)
    run(go)


# ---- P1: a condition lives only as long as the agent that set it ----
def test_a_condition_ends_when_its_agent_ends_and_a_neighbours_does_not():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "network", {"mode": "3g"})
        await d.agent_ended("b")  # another agent's goodbye changes nothing
        assert d._network == "3g"
        await d.agent_ended("a")
        assert d._network is None and d._network_by is None
        assert d.session.device.network_calls == [("set", "3g"), ("clear",)]
        await d.run_tool("c", "network", {"mode": "lossy"})  # (b has said goodbye: its id sets nothing any more)
        await d.agent_ended("a")  # a is long gone: c's condition stays
        assert d._network == "lossy"
        await finish(stop, task)
    run(go)


def test_a_condition_ends_when_its_agents_heartbeat_stops_but_not_before(monkeypatch):
    async def go():
        monkeypatch.setattr(daemon_module, "NETWORK_CHECK", 0.03)
        monkeypatch.setattr(daemon_module, "NETWORK_OWNER_LOST", 0.15)
        d = make()
        stop, task = await started(d)
        d.agent_alive("a")
        await d.run_tool("a", "network", {"mode": "3g"})
        for _ in range(6):  # alive: it keeps sending
            await asyncio.sleep(0.05)
            d.agent_alive("a")
        assert d._network == "3g"
        for _ in range(40):  # its process is killed: nothing is sent any more
            await asyncio.sleep(0.03)
            if d._network is None:
                break
        assert d._network is None and d.session.device.network_calls[-1] == ("clear",)
        # an agent that never sent a heartbeat (an HTTP client of the API) is left to the idle clear
        await d.run_tool("c", "network", {"mode": "3g"})
        await asyncio.sleep(0.4)
        assert d._network == "3g"
        await finish(stop, task)
    run(go)


def test_another_agent_replacing_or_ending_a_condition_tells_its_owner():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "network", {"mode": "3g"})
        await d.run_tool("b", "network", {"mode": "lossy"})
        text = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        assert text.endswith("Note: another agent set the network to lossy, replacing yours.")
        await d.agent_ended("a")  # the replaced agent's goodbye leaves b's condition alone
        assert d._network == "lossy" and d._network_by == "b"
        await d.run_tool("c", "network", {"mode": "normal"})
        assert d._network is None
        text = (await d.run_tool("b", "look", {"screenshot": False}))["text"]
        assert text.endswith("Note: another agent set the network back to normal.")
        await finish(stop, task)
    run(go)


def test_a_condition_left_by_a_crashed_daemon_is_cleared_when_the_next_daemon_starts():
    from tapwright import store

    async def go():
        d = make()
        stop, task = await started(d)
        assert d.session.device.network_calls == []  # no marker: nothing to clear
        await d.run_tool("a", "network", {"mode": "3g"})
        assert json.loads(store.NETWORK_FILE.read_text("utf-8"))["mode"] == "3g"  # noted on disk while it is on
        await d.run_tool("a", "network", {"mode": "normal"})
        assert not store.NETWORK_FILE.exists()
        await d.run_tool("a", "network", {"mode": "lossy"})
        await finish(stop, task)  # (this daemon is "killed": nothing cleared the marker)
        store.NETWORK_FILE.parent.mkdir(parents=True, exist_ok=True)
        store.NETWORK_FILE.write_text(json.dumps({"mode": "lossy", "pid": 1}), encoding="utf-8")
        fresh = make()
        stop, task = await started(fresh)
        assert fresh.session.device.network_calls == [("leftover",)]  # cleared before any agent's first call
        assert not store.NETWORK_FILE.exists()
        await finish(stop, task)
    run(go)


def test_a_leftover_that_cannot_be_cleared_is_tried_again_at_the_next_connect():
    from tapwright import store

    async def go():
        store.NETWORK_FILE.parent.mkdir(parents=True, exist_ok=True)
        store.NETWORK_FILE.write_text("{}", encoding="utf-8")
        d = make()

        async def broken():
            raise RuntimeError("no instruments")
        d.session.device.clear_leftover = broken
        stop, task = await started(d)
        assert d.state == "live" and store.NETWORK_FILE.exists()
        await finish(stop, task)
    run(go)


# ---- the MCP process tells the daemon it lives, and that it ends ----
def test_the_daemon_takes_the_heartbeat_and_the_goodbye_over_http():
    from starlette.testclient import TestClient
    from test_server import AUTH, TOKEN, app_and_daemon

    app, d = app_and_daemon()
    with TestClient(app) as c:
        assert c.post("/api/session/ping", json={}).status_code == 401
        assert c.post("/api/session/ping", json={}, headers=AUTH).json() == {"ok": True}
        assert "agent-1" in d._agent_alive
        assert c.post("/api/session/nonsense", json={}, headers=AUTH).status_code == 404
        assert c.post("/api/session/ping", json={}, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 400
        assert c.post("/api/network", json={"mode": "3g"}, headers=AUTH).status_code == 200
        assert d._network == "3g"
        assert c.post("/api/session/end", json={}, headers=AUTH).json() == {"ok": True}
        assert d._network is None and "agent-1" not in d._agent_alive


def test_the_mcp_process_sends_heartbeats_and_a_goodbye_when_its_stdio_closes(monkeypatch):
    import time

    from tapwright import client, mcp_server

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(client, "ping", lambda cid: seen.append(("ping", cid)) or True)
    monkeypatch.setattr(client, "end_session", lambda cid: seen.append(("end", cid)) or True)
    monkeypatch.setattr(mcp_server, "HEARTBEAT", 0.02)

    def serving(fn):  # the stdio server: it lives a moment, then stdin reaches its end
        time.sleep(0.1)
    monkeypatch.setattr(mcp_server.device, "run", serving)
    mcp_server.run(apps=["com.example.app"])
    assert ("ping", mcp_server.CLIENT_ID) in seen and seen[-1] == ("end", mcp_server.CLIENT_ID)
    count = len(seen)
    time.sleep(0.1)
    assert len(seen) == count  # the heartbeat stopped with the process

    seen.clear()  # the host interrupts it: the goodbye is still sent

    def interrupted(fn):
        raise KeyboardInterrupt()
    monkeypatch.setattr(mcp_server.device, "run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        mcp_server.run(apps=["com.example.app"])
    assert ("end", mcp_server.CLIENT_ID) in seen


def test_a_real_mcp_process_says_goodbye_when_its_stdin_reaches_its_end(tmp_path):
    """The real `tapwright mcp` as a child, with a stand-in daemon on the port its serve file names."""
    import http.server
    import os
    import subprocess
    import sys
    import threading
    import time

    heard: list[tuple[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            heard.append((self.path, self.headers.get("X-Tapwright-Client", "")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    home = tmp_path / "home"
    home.mkdir()
    (home / "serve.json").write_text(json.dumps({"port": httpd.server_address[1], "token": "t" * 43}), encoding="utf-8")
    env = {**os.environ, "TAPWRIGHT_HOME": str(home)}
    proc = subprocess.Popen([sys.executable, "-m", "tapwright", "mcp", "--apps", "com.example.app"], env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while not any(p == "/api/session/ping" for p, _ in heard) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert any(p == "/api/session/ping" for p, _ in heard), proc.poll()
        proc.stdin.close()  # the agent host has gone
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
        httpd.shutdown()
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
    ends = [cid for p, cid in heard if p == "/api/session/end"]
    pings = {cid for p, cid in heard if p == "/api/session/ping"}
    assert len(ends) == 1 and ends[0] in pings and len(ends[0]) == 32
