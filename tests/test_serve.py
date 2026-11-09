"""A real `serve()` on a free port with a fake session, found and driven through client.py."""
import logging
import threading
import time

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
    root = logging.getLogger()
    before = list(root.handlers)
    yield tmp_path
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    for handler in before:
        if handler not in root.handlers:
            root.addHandler(handler)


def test_serve_runs_is_found_and_stops(home):
    made = []

    def factory(log, phone):
        d = Daemon(FakeSession(), log=log, heartbeat=0.05, backoff=(0.01,), stream_linger=0, stream_retry=0.01)
        made.append(d)
        return d
    result = {}
    thread = threading.Thread(target=lambda: result.update(code=run(lambda: server.serve(port=0, make_daemon=factory))))
    thread.start()
    info = {}
    for _ in range(100):
        info = store.read_serve_info()
        if client.healthy(info):
            break
        time.sleep(0.1)
    assert client.healthy(info)["protocol"] == 1 and len(info["token"]) >= 32
    assert run(lambda: server.serve(port=0, make_daemon=factory)) == 0  # already running: found, not doubled
    assert len(made) == 1
    assert client.call("look", {"screenshot": False}, "agent-1")["texts"][0].startswith("screen")
    made[0].control.set_paused(True)
    with pytest.raises(client.DaemonError) as e:
        client.call("tap", {"x": 1, "y": 1}, "agent-1")
    assert e.value.code == "paused"
    client.shutdown(info)
    thread.join(10)
    assert result["code"] == 0 and store.read_serve_info()["token"] == info["token"]  # kept for next time
    assert info["token"] not in (home / "serve.log").read_text(encoding="utf-8")


def test_nothing_running():
    assert client.healthy({}) is None
    assert client.healthy({"port": 9, "token": "x"}) is None
