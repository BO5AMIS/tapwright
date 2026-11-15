"""An explicit runner identity failure must not turn into an unchecked ordinary tap."""
import asyncio
import json
import types

import pytest

from tapwright.actionlog import ActionLog
from tapwright.control import Refused
from tapwright.device import ConnectionLost, DeviceError
from tapwright.session import HIT_IDENTITY_REFUSED, Session
from test_171_button_content import setup
from test_daemon import finish, run, started
from test_review_17_c4_touches import real_dispatch
from test_runner_v5 import Runner, ready


def test_runner_identity_failure_is_terminal_and_contains_no_private_error_text():
    private = "UNRELATED-SAVED-IDENTITY@example.invalid"
    runner = Runner(hits=DeviceError(f"WebDriverAgent: unsupported operation: {HIT_IDENTITY_REFUSED} {private}"))
    session = ready(runner)
    with pytest.raises(Refused) as error:
        asyncio.run(session.hit_at(215, 792))
    assert error.value.code == "not_actionable" and private not in str(error.value)
    assert error.value.hit_check == {"gate": "identity_unproven", "phase": "runner", "probes": 0}
    assert not any(method == "POST" and path.endswith("/actions") for method, path, _ in runner.requests)


@pytest.mark.parametrize("when,dry_run", [("initial", False), ("initial", True), ("delivery", False)])
@pytest.mark.parametrize("failure", ["identity", "timeout", "missing", "generic", "malformed", "no_proof"])
def test_identity_refusal_prevents_input_and_retains_diagnostic(tmp_path, monkeypatch, when, dry_run, failure):
    async def go():
        d, target, _, direct = setup(placement="inside")
        d.actions = ActionLog(tmp_path)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            s.hit_at = types.MethodType(Session.hit_at, s)
            s.tw_version = 6
            s.device.rsd = object()
            checking_delivery = [False]
            old_http = s.device.http

            async def alive():
                return True

            s.device.wda_alive = alive

            async def http(method, path, payload=None, timeout=30):
                if path.startswith("/wda/tw/hit"):
                    value = {"t": direct.type, "n": direct.name, "l": direct.label,
                             "r": list(direct.rect), "b": direct.bundle, "identity": "verified"}
                    if when == "initial" or checking_delivery[0]:
                        if failure == "identity":
                            raise DeviceError(f"WebDriverAgent: unsupported operation: {HIT_IDENTITY_REFUSED}")
                        if failure == "timeout":
                            raise ConnectionLost("the phone didn't answer within 5 s")
                        if failure == "missing":
                            raise DeviceError("WebDriverAgent: unsupported operation: tapwright hit: no element answered")
                        if failure == "generic":
                            raise RuntimeError("private runner exception must not be shown")
                        if failure == "malformed":
                            return {"value": {"identity": "verified"}}
                        value.pop("identity")
                    return {"value": value}
                return await old_http(method, path, payload, timeout)

            async def notify(ms):
                checking_delivery[0] = True

            s.device.http = http
            monkeypatch.setattr(d._agent_touches, "notify", notify)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"ref": target.ref}, "dry_run": dry_run,
                                                "screenshot": False})
            expected = {"identity": "identity_unproven", "timeout": "hit_timeout", "missing": "hit_request_failed",
                        "generic": "hit_request_failed", "malformed": "invalid_hit_proof", "no_proof": "invalid_hit_proof"}
            assert not s.dispatched and error.value.hit_check["gate"] == expected[failure]
            if when == "delivery":
                assert checking_delivery[0]
            entries = [json.loads(line) for p in tmp_path.glob("actions-*.jsonl")
                       for line in p.read_text(encoding="utf-8").splitlines()]
            assert entries[-1]["hit_check"] == error.value.hit_check
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("reply", [None, {}, {"value": {}}, {"value": {"tapwright": "6"}},
                                  {"value": {"tapwright": True}}, {"value": {"tapwright": -1}},
                                  "timeout", "error", "verified"])
@pytest.mark.parametrize("prior_probe", [False, True])
def test_failed_capability_discovery_never_disables_hit_proof(reply, prior_probe):
    async def go():
        d, target, _, direct = setup(placement="inside")
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            s.hit_at = types.MethodType(Session.hit_at, s)
            s.fast_tap, s.fast_drag, s.tw_version = None, None, 0
            old_http = s.device.http
            hit_requests = []

            async def http(method, path, payload=None, timeout=30):
                if path.endswith("/wda/tw/version"):
                    if reply == "timeout":
                        raise DeviceError("the capability request timed out")
                    if reply == "error":
                        raise RuntimeError("private exception must not be shown")
                    return {"value": {"tapwright": 6}} if reply == "verified" else reply
                if path.startswith("/wda/tw/hit"):
                    hit_requests.append(path)
                    return {"value": {"t": direct.type, "n": direct.name, "l": direct.label,
                                      "r": list(direct.rect), "b": direct.bundle, "identity": "verified"}}
                return await old_http(method, path, payload, timeout)

            s.device.http = http
            if prior_probe:
                try:
                    await s._fast_routes()
                except RuntimeError:
                    pass
            call = d.run_tool("a", "tap", {"target": {"ref": target.ref}, "screenshot": False})
            if reply == "verified":
                await call
                assert len(s.dispatched) == 1 and len(hit_requests) == 2
            else:
                with pytest.raises(Refused) as error:
                    await call
                assert error.value.hit_check["gate"] == "capability_unproven"
                assert not s.dispatched and not hit_requests and s.fast_tap is None
        finally:
            await finish(stop, task)
    run(go)
