"""A real `serve()` on a free port with a fake session, found and driven through client.py."""
import http.server
import json
import logging
import socket
import threading
import time
import urllib.request

import pytest
from fakes import FakeSession

from tapwright import client, server, store
from tapwright.daemon import Daemon
from tapwright.device import run


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOME", tmp_path)
    monkeypatch.setattr(store, "SERVE_FILE", tmp_path / "serve.json")
    monkeypatch.setattr(client, "start_daemon", lambda: pytest.fail("the running daemon should be found"))
    monkeypatch.setattr(client, "SCOPE", {})  # each test starts in personal mode with no apps, as a process does
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    mobile = logging.getLogger("pymobiledevice3")
    mobile_level = mobile.level
    yield tmp_path
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    for handler in before:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)
    mobile.setLevel(mobile_level)


def make_factory(made):
    def factory(log, phone):
        d = Daemon(FakeSession(), log=log, heartbeat=0.05, backoff=(0.01,), stream_linger=0, stream_retry=0.01)
        made.append(d)
        return d
    return factory


def start_serve(factory, result):
    def target():
        try:
            result.update(code=run(lambda: server.serve(port=0, make_daemon=factory)))
        except BaseException as e:  # reported by the test, not lost in the thread
            result.update(error=e)
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    info = {}
    for _ in range(100):
        info = store.read_serve_info()
        if client.healthy(info):
            break
        time.sleep(0.1)
    return thread, info


def test_serve_runs_is_found_and_stops(home):
    made, result = [], {}
    thread, info = start_serve(make_factory(made), result)
    try:
        assert client.healthy(info)["protocol"] == 1 and len(info["token"]) >= 32
        assert run(lambda: server.serve(port=0, make_daemon=make_factory(made))) == 0  # found, not doubled
        assert len(made) == 1
        with pytest.raises(client.DaemonError) as e:  # personal mode with no app list: nothing runs
            client.call("look", {"screenshot": False}, "agent-1")
        assert e.value.code == "not_allowed" and str(e.value).startswith("no_apps: ")
        client.set_scope(dedicated=True)
        assert client.call("look", {"screenshot": False}, "agent-1")["text"].startswith("Screen")
        made[0].control.set_paused(True)
        with pytest.raises(client.DaemonError) as e:
            client.call("tap", {"target": {"x": 1, "y": 1}}, "agent-1")
        assert e.value.code == "paused"
    finally:
        if client.healthy(info):
            client.shutdown(info)
        thread.join(10)
    assert "error" not in result and result["code"] == 0
    assert store.read_serve_info()["token"] == info["token"]  # kept for next time
    assert info["token"] not in (home / "serve.log").read_text(encoding="utf-8")


def test_stop_with_an_open_stream_does_not_hang(home):
    made, result = [], {}
    thread, info = start_serve(make_factory(made), result)
    opened = threading.Event()

    def watch():
        req = urllib.request.Request(client.base_url(info) + "/stream",
                                     headers={"Authorization": f"Bearer {info['token']}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                opened.set()
                while r.read(1024):
                    pass
        except OSError:
            pass
    threading.Thread(target=watch, daemon=True).start()
    try:
        assert opened.wait(10)
        time.sleep(0.3)
    finally:
        if client.healthy(info):
            client.shutdown(info)
        thread.join(8)
    assert not thread.is_alive()
    assert "error" not in result and result["code"] == 0


def test_a_daemon_that_drops_the_line_while_stopping_is_not_a_failed_stop(monkeypatch):
    """`serve --stop` asked a daemon with an open stream to stop, and now and then the connection was reset
    before the answer came: the stop had worked, and the command ended in a traceback."""
    def reset(*a, **k):
        raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")
    monkeypatch.setattr(client.urllib.request, "urlopen", reset)
    client.shutdown({"port": 1, "token": "t"})  # no exception

    def refused(*a, **k):
        raise client.DaemonError("unauthorized", "missing or wrong token")
    monkeypatch.setattr(client, "_request", refused)
    with pytest.raises(client.DaemonError):
        client.shutdown({"port": 1, "token": "t"})


def test_the_lock_keeps_a_second_daemon_out(home):
    made = []
    lock = server._lock_instance()
    assert lock is not None
    try:
        assert server._lock_instance() is None
        assert run(lambda: server.serve(port=0, make_daemon=make_factory(made), wait_for_other=0.5)) == 1
        assert made == []
    finally:
        lock.close()
    again = server._lock_instance()
    assert again is not None  # released
    again.close()


def test_a_restart_right_after_a_stop_takes_over(home):
    made, first, second = [], {}, {}
    thread, info = start_serve(make_factory(made), first)
    assert client.healthy(info)
    thread2 = None
    try:
        client.shutdown(info)
        for _ in range(100):  # gone from the network, but maybe still holding the lock
            if client.healthy(store.read_serve_info()) is None:
                break
            time.sleep(0.05)

        def again():
            try:
                second.update(code=run(lambda: server.serve(port=0, make_daemon=make_factory(made),
                                                            wait_for_other=15)))
            except BaseException as e:
                second.update(error=e)
        thread2 = threading.Thread(target=again, daemon=True)
        thread2.start()
        info2 = {}
        for _ in range(150):
            info2 = store.read_serve_info()
            if client.healthy(info2):
                break
            time.sleep(0.1)
        assert client.healthy(info2)
        assert len(made) == 2
    finally:
        current = store.read_serve_info()
        if client.healthy(current):
            client.shutdown(current)
        thread.join(10)
        if thread2 is not None:
            thread2.join(10)
    assert "error" not in first and "error" not in second and second["code"] == 0


class Slow(http.server.BaseHTTPRequestHandler):
    seen = 0

    def do_POST(self):
        type(self).seen += 1
        time.sleep(1.5)

    def log_message(self, *args):
        pass


def test_a_timeout_is_not_retried(monkeypatch):
    Slow.seen = 0
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    info = {"port": httpd.server_address[1], "token": "t" * 32}
    monkeypatch.setattr(client, "find_or_start", lambda: info)
    try:
        with pytest.raises(client.DaemonError) as e:
            client.call("tap", {"target": {"x": 1, "y": 1}}, "agent-1", timeout=0.3)
        assert e.value.code == "timeout"
        assert Slow.seen == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def raw_server(reply: bytes):
    """Answers every connection with `reply` (maybe nothing) and closes it."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(1)
                try:
                    conn.recv(4096)
                    conn.sendall(reply)
                except OSError:
                    pass
    threading.Thread(target=loop, daemon=True).start()
    return srv


def test_healthy_rejects_odd_answers():
    body = json.dumps([1, 2]).encode()
    listing = raw_server(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
    cut = raw_server(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{")  # drops mid-response
    silent = raw_server(b"")
    try:
        for srv in (listing, cut, silent):
            assert client.healthy({"port": srv.getsockname()[1], "token": "x"}) is None
    finally:
        for srv in (listing, cut, silent):
            srv.close()


def test_nothing_running():
    assert client.healthy({}) is None
    assert client.healthy({"port": 9, "token": "x"}) is None
