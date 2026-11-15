"""Review regressions: refreshed ancestry and fresh hard bars cannot authorize recovery."""
import asyncio
import dataclasses
import xml.etree.ElementTree as ET

import pytest

from tapwright import ui
from tapwright.control import Refused
from test_171_button_content import SHAPES, setup
from test_daemon import finish, run, started
from test_dialog import asked
from test_guessed_button_cover import changed_source, navbar
from test_review_17_c4_touches import real_dispatch


@pytest.mark.parametrize("change", ["inside", "disabled", "alert", "nested_action"])
def test_sibling_becoming_a_descendant_never_escapes_exact_recovery(change):
    async def go():
        d, _, _, _ = setup(placement="sibling")
        fresh, _, _, _ = setup(placement="inside", enabled=change != "disabled")
        source = fresh.session.last_source
        if change == "alert":
            source = changed_source(source, "alert")
        elif change == "nested_action":
            root = ET.fromstring(source)
            control = next(n for n in root.iter() if n.get("name") == "control")
            child = list(control)[0]
            control.remove(child)
            nested = ET.SubElement(control, "XCUIElementTypeButton", type="XCUIElementTypeButton",
                                   name="post", label="Post", enabled="true", x="28", y="594",
                                   width="374", height="49")
            nested.append(child)
            source = ET.tostring(root, encoding="unicode")
        d.session.source_now = source
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change", ["unanswered", "holder", "nested_action", "exact",
                                    "exact_then_changed", "exact_then_disabled"])
def test_inline_confirmation_cannot_downgrade_recovery_proof(change, monkeypatch):
    async def go():
        d, target, passive, direct = setup(SHAPES[2], placement="sibling")
        stop, task = await started(d)
        call = None
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            notified = []

            async def notify(ms):
                notified.append(ms)
                if change == "exact_then_changed":
                    s.center_answer = dataclasses.replace(direct, label="Delete account")
                elif change == "exact_then_disabled":
                    s.source_now = changed_source(s.last_source, "disabled")

            monkeypatch.setattr(d._agent_touches, "notify", notify)
            call = asyncio.create_task(d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False},
                                                  dialog=True))
            request, _ = await asked(d)
            assert not s.dispatched and any(p != target.center for p in s.hit_asks)
            if change == "unanswered":
                s.center_answer = None
            elif change == "holder":
                s.center_answer = ui.Hit("Window", "", "", (0, 0, 430, 932), direct.bundle)
            elif change == "nested_action":
                fresh, _, _, _ = setup(SHAPES[2], placement="inside")
                root = ET.fromstring(fresh.session.last_source)
                control = next(n for n in root.iter() if n.get("name") == "control")
                child = list(control)[0]
                control.remove(child)
                nested = ET.SubElement(control, "XCUIElementTypeButton", type="XCUIElementTypeButton",
                                       name="post", label="Post", enabled="true", x="28", y="246",
                                       width="374", height="45")
                nested.append(child)
                s.last_source = s.source_now = ET.tostring(root, encoding="unicode")
                s.tree = ui.read(s.last_source, s.screen).items
                s.center_answer = passive
            else:
                s.center_answer = direct

            assert d.dialog_answer("a", request["id"], "allow")
            if change == "exact":
                await asyncio.wait_for(call, 3)
                [(_, payload)] = s.dispatched
                assert (payload["x"], payload["y"]) == target.center
            else:
                with pytest.raises(Refused):
                    await asyncio.wait_for(call, 3)
                assert not s.dispatched
                if change.startswith("exact_then"):
                    assert notified
        finally:
            if call is not None and not call.done():
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("when", ["refresh", "delivery"])
@pytest.mark.parametrize("kind", ["Toolbar", "Keyboard", "TabBar"])
def test_new_hard_cover_blocks_even_an_exact_button_hit(when, kind, monkeypatch):
    async def go():
        d, _, _, _, _ = navbar()
        root = ET.fromstring(d.session.last_source)
        window = next(n for n in root.iter() if n.get("type") == "XCUIElementTypeWindow")
        ET.SubElement(window, "XCUIElementType" + kind, type="XCUIElementType" + kind,
                      x="0", y="75", width="430", height="65")
        covered = ET.tostring(root, encoding="unicode")
        if when == "refresh":
            d.session.source_now = covered
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            if when == "delivery":
                async def notify(ms):
                    s.source_now = covered
                monkeypatch.setattr(d._agent_touches, "notify", notify)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched
        finally:
            await finish(stop, task)
    run(go)
