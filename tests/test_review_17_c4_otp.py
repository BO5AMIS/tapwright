"""External OTP calls take part in scheduled-run admission, including cleanup."""
import asyncio
import json
import time

import pytest
from starlette.requests import Request

from test_daemon import finish, run, started
from test_notifications import BUNDLE, CHAT, handover_daemon
from test_otp import A, TOKEN
from tapwright import center_tool, schedules
from tapwright.control import Refused
from tapwright.daemon import IN_USE_S, Scope


async def post(app, path, data):
    body = json.dumps(data).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": path, "scheme": "http",
                       "headers": [(b"host", b"testserver"), (b"authorization", f"Bearer {TOKEN}".encode())],
                       "client": A, "server": ("testserver", 80), "query_string": b""}, receive)
    return await next(route.endpoint for route in app.routes if route.path == path)(request)


@pytest.mark.parametrize("cancel", [False, True])
def test_external_otp_blocks_a_stale_idle_schedule_and_the_quiet_period(tmp_path, monkeypatch, cancel):
    for name in ("CENTER_OPEN_WAIT", "CENTER_CLOSE_WAIT", "OPEN_WAIT"):
        monkeypatch.setattr(center_tool, name, 0)

    async def go():
        app, d, session = handover_daemon(tmp_path)
        stop, task = await started(d)
        held = None
        entered, release = asyncio.Event(), asyncio.Event()
        original = d._read_center

        async def read(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        d._read_center = read
        try:
            assert not d.status()["in_use"]  # scheduler's status read predates /otp
            assert (await post(app, "/api/prove", {"challenge": "ab" * 32})).status_code == 200
            held = asyncio.create_task(post(app, "/otp", {"app": BUNDLE}))
            await asyncio.wait_for(entered.wait(), 2)
            # The first drive must refuse now, before it can queue an open_app behind the handover.
            with pytest.raises(Refused) as refused:
                await asyncio.wait_for(d.run_tool("schedule-other", "drive", {"route": "missing"},
                                                  scope=Scope((CHAT,))), 0.25)
            assert refused.value.code == "busy" and str(refused.value) == schedules.ADMISSION_BUSY
            assert session.foreground == BUNDLE and not d.scheduled_running()
            if cancel:
                held.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await held
            else:
                release.set()
                assert (await held).status_code == 200
            assert not d._calls  # no ownership leak on success or cancellation
            with pytest.raises(Refused) as refused:
                await d.run_tool("schedule-other", "look", {"screenshot": False})
            assert refused.value.code == "busy"
            d._call_at = {client: time.monotonic() - IN_USE_S - 1 for client in d._call_at}
            assert (await d.run_tool("schedule-other", "look", {"screenshot": False}))["text"]
        finally:
            release.set()
            if held is not None and not held.done():
                held.cancel()
                await asyncio.gather(held, return_exceptions=True)
            await finish(stop, task)
    run(go)


def test_external_otp_cannot_interrupt_an_admitted_schedule(tmp_path):
    async def go():
        app, d, session = handover_daemon(tmp_path)
        d._calls["schedule-other"] = 1
        assert (await post(app, "/api/prove", {"challenge": "ab" * 32})).status_code == 200
        response = await post(app, "/otp", {"app": BUNDLE})
        assert response.status_code == 409 and json.loads(response.body)["error"]["code"] == "busy"
        assert session.center_reads == 0 and d._calls == {"schedule-other": 1}
    run(go)
