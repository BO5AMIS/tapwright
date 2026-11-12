"""AGENT-TOOLS §16.3 and §16.4: `open_app` with a language, region and text size, and `network` conditions."""
import asyncio
import json

import pytest
from test_daemon import finish, make, run, started

from tapwright import daemon as daemon_module, launch, network, params
from tapwright.control import Refused
from tapwright.daemon import Scope
from tapwright.device import ConnectionLost, DeviceError


# ---- open_app: language, region, text size ----
def test_the_launch_arguments_are_apples_own():
    assert launch.arguments({"language": "ar", "region": "QA", "text_size": "xl"}) == [
        "-AppleLanguages", "(ar)", "-AppleLocale", "ar_QA",
        "-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryXL"]
    assert launch.arguments({"language": "en-GB"}) == ["-AppleLanguages", "(en-GB)"]
    assert launch.arguments({"region": "QA"}) == ["-AppleLocale", "en_QA"]  # a region alone: with English
    assert launch.arguments({"language": "zh-Hans", "region": "CN"})[-1] == "zh_CN"
    assert launch.arguments({}) == []


def test_every_text_size_maps_to_a_content_size_category():
    names = {"xs": "XS", "s": "S", "m": "M", "l": "L", "xl": "XL", "xxl": "XXL", "xxxl": "XXXL",
             "ax1": "AccessibilityM", "ax2": "AccessibilityL", "ax3": "AccessibilityXL", "ax4": "AccessibilityXXL",
             "ax5": "AccessibilityXXXL"}
    assert list(launch.TEXT_SIZES) == list(names)
    for size, name in names.items():
        assert launch.arguments({"text_size": size}) == ["-UIPreferredContentSizeCategoryName",
                                                         f"UICTContentSizeCategory{name}"]


def test_values_are_cleaned_and_validated():
    assert launch.check({"language": " ar ", "region": "qa", "text_size": "XL"}) == {
        "language": "ar", "region": "QA", "text_size": "xl"}
    assert launch.check({"language": None, "region": None, "text_size": None}) == {}
    for args, word in (({"language": "arabic!"}, "language"), ({"language": 5}, "language"),
                       ({"region": "QAT"}, "region"), ({"region": ""}, "region"),
                       ({"text_size": "huge"}, "text_size"), ({"text_size": 3}, "text_size")):
        with pytest.raises(Refused) as e:
            launch.check(args)
        assert e.value.code == "bad_params" and e.value.args[0].startswith(word)
    with pytest.raises(Refused) as e:
        launch.check({"text_size": "huge"})
    assert "xs, s, m, l, xl, xxl, xxxl, ax1, ax2, ax3, ax4, ax5" in str(e.value)  # the allowed list


def test_said_names_only_the_parts_given():
    assert launch.said({"language": "ar", "region": "QA", "text_size": "xl"}) == " in ar, QA, text size xl"
    assert launch.said({"text_size": "ax3"}) == " in text size ax3"
    assert launch.said({"region": "QA"}) == " in QA"
    assert launch.said({}) == ""


def test_open_app_launches_with_the_arguments_and_says_so():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "open_app", {"app": "com.example.app", "language": "ar", "region": "qa",
                                              "text_size": "xl"})
        assert r["text"].splitlines()[0] == "Opened Example (com.example.app) in ar, QA, text size xl."
        assert d.session.launch_args == [["-AppleLanguages", "(ar)", "-AppleLocale", "ar_QA",
                                          "-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryXL"]]
        r = await d.run_tool("a", "open_app", {"app": "com.example.app", "text_size": "ax5", "look": False})
        assert r["text"] == "Opened Example (com.example.app) in text size ax5."
        await finish(stop, task)
    run(go)


def test_a_running_app_is_closed_first_and_a_plain_open_is_not_touched():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "open_app", {"app": "com.example.app", "look": False})
        assert d.session.closed == [] and d.session.launch_args == [[]]  # as before: no arguments, no close
        await d.run_tool("a", "open_app", {"app": "com.example.app", "language": "ar", "look": False})
        assert d.session.closed == ["com.example.app"]
        calls = [c for c in d.session.calls if c in ("terminate", "launch")]
        assert calls == ["launch", "terminate", "launch"]  # closed, then launched with the arguments
        assert d.session.launch_args[-1] == ["-AppleLanguages", "(ar)"]
        await finish(stop, task)
    run(go)


def test_bad_values_are_refused_before_anything_reaches_the_phone():
    async def go():
        d = make()
        stop, task = await started(d)
        for args in ({"language": "not a code"}, {"region": "Qatar"}, {"text_size": "huge"}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "open_app", {"app": "com.example.app", **args})
            assert e.value.code == "bad_params"
        assert d.session.launched == [] and d.session.closed == []
        await finish(stop, task)
    run(go)


def test_a_failed_close_of_a_not_running_app_does_not_stop_the_launch():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.fail["terminate"] = DeviceError("not running")
        r = await d.run_tool("a", "open_app", {"app": "com.example.app", "region": "QA", "look": False})
        assert r["text"] == "Opened Example (com.example.app) in QA."
        assert d.session.launch_args == [["-AppleLocale", "en_QA"]]
        await finish(stop, task)
    run(go)


def test_personal_mode_still_gives_only_the_given_apps():
    async def go():
        d = make()
        stop, task = await started(d)
        mine = Scope(("com.example.app",))
        r = await d.run_tool("a", "open_app", {"app": "com.example.app", "language": "ar", "look": False}, scope=mine)
        assert r["text"] == "Opened Example (com.example.app) in ar."
        d.session.installed.append({"bundle_id": "com.other.app", "name": "Other", "version": "1", "build": "1",
                                    "executable": "Other"})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "open_app", {"app": "com.other.app", "language": "ar"}, scope=mine)
        assert e.value.code == "not_allowed" and d.session.launched == ["com.example.app"]
        await finish(stop, task)
    run(go)


def test_recording_keeps_the_arguments_on_the_open_app_step(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "record", {"start": True, "name": "arabic"})
        await d.run_tool("a", "open_app", {"app": "com.example.app", "language": "ar", "region": "QA",
                                          "text_size": "xl", "look": False})
        path = tmp_path / "s.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        steps = json.loads(path.read_text("utf-8"))["steps"]
        assert steps == [{"tool": "open_app", "app": "com.example.app", "language": "ar", "region": "QA",
                          "text_size": "xl"}]
        d.session.launch_args.clear()
        await d.run_tool("a", "replay", {"path": str(path)})
        assert d.session.launch_args[-1][:2] == ["-AppleLanguages", "(ar)"]  # a replay opens it the same way
        await finish(stop, task)
    run(go)


def test_open_app_takes_them_in_run_and_in_the_table():
    assert {"language", "region", "text_size"} <= set(params.ALLOWED["open_app"])

    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "run", {"steps": [{"tool": "open_app", "app": "com.example.app", "text_size": "m"}]})
        assert r["text"].startswith("Ran 1 of 1 steps")
        assert d.session.launch_args[-1] == ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryM"]
        await finish(stop, task)
    run(go)


# ---- network ----
GROUPS = [{"identifier": "SlowNetworkCondition", "profiles": [
    {"identifier": "SlowNetwork100PctLoss", "description": "100% Loss"},
    {"identifier": "SlowNetworkVeryBadNetwork", "description": "Very Bad Network"},
    {"identifier": "SlowNetwork3GGood", "description": "3G Good"},
    {"identifier": "SlowNetworkEdgeGood", "description": "Edge Good"}]}]


def test_the_profile_is_chosen_per_mode_from_the_phones_own_list():
    assert network.pick_profile("offline", GROUPS) == "SlowNetwork100PctLoss"
    assert network.pick_profile("3g", GROUPS) == "SlowNetwork3GGood"
    assert network.pick_profile("lossy", GROUPS) == "SlowNetworkVeryBadNetwork"
    odd = [{"identifier": "X", "profiles": [{"identifier": "NetA", "description": "3G Bad"},
                                            {"identifier": "NetB", "description": "100% Loss"}]}]
    assert network.pick_profile("3g", odd) == "NetA" and network.pick_profile("offline", odd) == "NetB"
    assert network.pick_profile("lossy", odd) is None
    assert network.pick_profile("3g", []) is None and network.pick_profile("3g", None) is None


def test_network_answers_one_line_and_every_header_carries_it():
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "network", {"mode": "3g"})
        assert r == {"text": "Network: 3g (Tapwright, whole phone).", "image": None, "image_size": None}
        assert d.session.device.network_calls == [("set", "3g")]
        look = (await d.run_tool("a", "look", {"screenshot": False}))["text"].splitlines()
        assert look[0].startswith("Screen ") and look[1] == "Network: 3g (Tapwright, whole phone)."
        tap = (await d.run_tool("a", "tap", {"target": {"ref": "e1"}, "look": False}))["text"].splitlines()
        assert tap[0].startswith("Tapped e1") and tap[1] == "Network: 3g (Tapwright, whole phone)."
        r = await d.run_tool("a", "network", {"mode": "offline"})  # replaces it
        assert r["text"] == "Network: offline (Tapwright, whole phone)."
        assert (await d.run_tool("a", "look", {"screenshot": False}))["text"].splitlines()[1] == \
            "Network: offline (Tapwright, whole phone)."
        await finish(stop, task)
    run(go)


def test_no_line_without_a_condition_and_normal_clears_it():
    async def go():
        d = make()
        stop, task = await started(d)
        assert "Network:" not in (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        await d.run_tool("a", "network", {"mode": "lossy"})
        r = await d.run_tool("a", "network", {"mode": "normal"})
        assert r["text"] == "Network: normal (Tapwright, whole phone)."
        assert d.session.device.network_calls == [("set", "lossy"), ("clear",)]
        assert "Network:" not in (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        await finish(stop, task)
    run(go)


def test_a_bad_mode_is_bad_params_and_a_missing_one_too():
    async def go():
        d = make()
        stop, task = await started(d)
        for args in ({"mode": "fast"}, {"mode": 3}, {}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "network", args)
            assert e.value.code == "bad_params"
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "network", {"mode": "3g", "speed": 1})
        assert "doesn't take speed" in str(e.value)
        assert d.session.device.network_calls == []
        await finish(stop, task)
    run(go)


def test_offline_and_lossy_are_unsupported_over_wifi_but_3g_is_not():
    async def go():
        d = make()
        d.session.device.via = "local network"
        stop, task = await started(d)
        for mode in ("offline", "lossy"):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "network", {"mode": mode})
            assert e.value.code == "unsupported" and "cut Tapwright's own link" in str(e.value)
        assert d.session.device.network_calls == []
        assert (await d.run_tool("a", "network", {"mode": "3g"}))["text"].startswith("Network: 3g")
        await d.run_tool("a", "network", {"mode": "normal"})  # always allowed
        await finish(stop, task)
    run(go)


def test_a_phone_without_the_profile_is_unsupported_and_leaves_no_condition():
    async def go():
        d = make()
        d.session.device.network_error = DeviceError("this phone offers no network condition for 3g")
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "network", {"mode": "3g"})
        assert e.value.code == "unsupported" and "offers no network condition" in str(e.value)
        assert "Network:" not in (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        await finish(stop, task)
    run(go)


def test_the_condition_ends_when_the_link_drops():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "network", {"mode": "3g"})
        d.lost(ConnectionLost("gone"))
        assert d.session.device.network_calls[-1] == ("drop",)
        for _ in range(300):
            if d.state == "live" and d._attempts > 1:
                break
            await __import__("asyncio").sleep(0.01)
        assert "Network:" not in (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        await finish(stop, task)
    run(go)


def test_the_condition_is_cleared_when_the_daemon_closes():
    async def go():
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "network", {"mode": "offline"})
        await finish(stop, task)
        await d.close()
        assert d.session.device.network_calls[-1] == ("clear",) and d._network is None
    run(go)


def test_network_is_a_step_in_run_and_is_logged_and_recorded(tmp_path):
    async def go():
        d = make()
        stop, task = await started(d)
        r = await d.run_tool("a", "run", {"steps": [{"tool": "network", "mode": "3g"},
                                                    {"tool": "tap", "target": {"id": "ok-button"}}]})
        assert r["text"].startswith("Ran 2 of 2 steps") and d.session.device.network_calls == [("set", "3g")]
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "run", {"steps": [{"tool": "network", "mode": "fast"}]})
        assert e.value.code == "bad_params" or "mode" in str(e.value)
        await d.run_tool("a", "record", {"start": True, "name": "slow"})
        await d.run_tool("a", "network", {"mode": "lossy"})
        path = tmp_path / "s.json"
        await d.run_tool("a", "record", {"stop": True, "path": str(path)})
        assert json.loads(path.read_text("utf-8"))["steps"] == [{"tool": "network", "mode": "lossy"}]
        await finish(stop, task)
    run(go)


def test_network_is_an_action_to_log_and_personal_mode_lets_it_through():
    from tapwright import daemon as daemon_module, batch
    assert "network" in daemon_module.LOGGED and "network" in batch.STEP_TOOLS
    assert batch.summary({"tool": "network", "mode": "3g"}) == "network 3g"

    async def go():
        d = make()
        stop, task = await started(d)
        mine = Scope(("com.example.app",))
        r = await d.run_tool("a", "network", {"mode": "3g"}, scope=mine)
        assert r["text"] == "Network: 3g (Tapwright, whole phone)."
        await finish(stop, task)
    run(go)


def test_a_condition_whose_agent_goes_quiet_is_cleared(monkeypatch):
    """§16.4: the phone goes back to normal when the session ends. The daemon outlives an agent's session, so a
    condition is cleared once the agent that set it has made no call for NETWORK_IDLE; other agents' calls don't count."""
    async def go():
        monkeypatch.setattr(daemon_module, "NETWORK_IDLE", 0.2)
        monkeypatch.setattr(daemon_module, "NETWORK_CHECK", 0.05)
        d = make()
        stop, task = await started(d)
        await d.run_tool("a", "network", {"mode": "3g"})
        for _ in range(4):  # the setter keeps calling: it stays
            await asyncio.sleep(0.1)
            await d.run_tool("a", "look", {"screenshot": False})
        assert d._network == "3g"
        for _ in range(5):  # only another agent calls: it goes
            await asyncio.sleep(0.1)
            await d.run_tool("b", "look", {"screenshot": False})
        assert d._network is None and d.session.device.network_calls[-1] == ("clear",)
        await finish(stop, task)
    run(go)


def test_text_size_system_is_the_phones_own():
    assert launch.check({"text_size": "system"}) == {}
    assert launch.check({"text_size": "SYSTEM", "language": "ar"}) == {"language": "ar"}
