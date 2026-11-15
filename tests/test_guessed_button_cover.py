"""A navbar's oversized sibling frame is only a cover guess, never touch authorization.

All geometry and runner responses here are synthetic. Real Session dispatch remains in use.
"""
import dataclasses
import xml.etree.ElementTree as ET

import pytest

from test_171_button_content import NO_CONFIRM, setup
from test_daemon import finish, run, started
from test_review_17_c1 import APP
from test_review_17_c4_touches import real_dispatch
from test_runner_v5 import node
from tapwright import daemon as daemon_module, ui
from tapwright.control import Refused


def navbar(*, label="Cancel", top=82, nested=False):
    rect = (16.4, top, 48, 49)
    content_rect = (32.4, top + 15, 16, 19)
    d, _, private, direct = setup((label, rect, "Other", "", content_rect))
    control = node("Button", rect, [node("Button", content_rect, n="post", l="Post")] if nested else (),
                   n="control", l=label)
    address = node("Button", (25.6, top, 378, 49), n="URL", l="Address")
    nav = node("NavigationBar", (0, top - 3, 430, 55), [control, address], n="Browser")
    source = ui.xml_of_tree(node("Application", (0, 0, 430, 932), [
        node("Window", (0, 0, 430, 932), [nav])], n="Example", b=APP))
    s = d.session
    s.last_source, s.tree = source, ui.read(source, s.screen).items
    target = next(e for e in s.tree if e.name == "control")
    address_hit = ui.Hit("Button", "URL", "Address", tuple(address["r"]), APP)
    return d, target, private, direct, address_hit


def changed_source(source, change):
    root = ET.fromstring(source)
    control = next(n for n in root.iter() if n.get("name") == "control")
    if change == "disabled":
        control.set("enabled", "false")
    elif change == "changed":
        control.set("label", "Delete account")
    elif change == "alert":
        window = next(n for n in root.iter() if n.get("type") == "XCUIElementTypeWindow")
        ET.SubElement(window, "XCUIElementTypeAlert", type="XCUIElementTypeAlert",
                      x="50", y="500", width="300", height="100")
    return ET.tostring(root, encoding="unicode")


def weak_hit(kind, direct, private):
    return {
        "unanswered": None,
        "holder": ui.Hit("Window", "", "", (0, 0, 430, 932), APP),
        "foreign": dataclasses.replace(direct, bundle="com.example.foreign"),
        "changed": dataclasses.replace(direct, label="Delete account"),
        "other_button": dataclasses.replace(direct, name="post", label="Post"),
        "private": private,
    }[kind]


def test_fractional_navbar_cover_recovers_only_a_proven_background_and_rechecks_it_at_dispatch():
    async def go():
        d, target, private, _, _ = navbar()
        original_covers = target.covered_by
        assert [b.kind for b in original_covers] == ["Button"]
        assert "@26,82 378x49" in original_covers[0].line
        assert ui.visible_part(target.rect, original_covers, d.session.screen)[2] == pytest.approx(9.2)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            result = await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            [(_, payload)] = s.dispatched
            point = (payload["x"], payload["y"])
            assert result["text"].startswith("Tapped")
            assert ui._holds(target.rect, point) and not ui._holds(private.rect, point)
            assert point != target.center and s.hit_asks[-1] == point
            assert s.hit_asks.count(point) >= 2  # selection and the actual POST each need their own proof
            assert s.calls.count("read_source") >= 2
            assert target.covered_by == original_covers  # no globally cleared geometry
            assert s.before_input is None
        finally:
            await finish(stop, task)
    run(go)


def test_cancel_recovery_dry_run_probes_but_never_sends_input():
    async def go():
        d, target, _, _, _ = navbar()
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            result = await d.run_tool("a", "tap", {"target": {"ref": target.ref}, "dry_run": True,
                                                    "screenshot": False})
            assert result["text"].startswith("Would tap")
            assert not s.dispatched and s.before_input is None
            assert any(point != target.center for point in s.hit_asks)
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("kind", ["NavigationBar", "Toolbar", "TabBar", "Keyboard", "StatusBar"])
def test_a_real_bar_still_blocks_recovery(kind):
    async def go():
        d, target, _, _, _ = navbar(top=0 if kind == "StatusBar" else 82)
        if kind != "StatusBar":
            target.covered_by += (ui.Bar(kind, target.rect, kind),)
        original_covers = target.covered_by
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched and set(s.hit_asks) <= {target.center}
            assert target.covered_by == original_covers
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("kind", ["Keyboard", "StatusBar"])
def test_recovery_candidates_keep_real_bars_away_from_the_center_excluded(kind):
    async def go():
        d, target, _, direct, _ = navbar(top=40 if kind == "StatusBar" else 82)
        if kind == "Keyboard":
            bar = ui.Bar(kind, (16.4, 82, 17, 49), kind)
            target.covered_by += (bar,)
        else:
            bar = ui.Bar(kind, (0, 0, 430, daemon_module.STATUS_BAR), kind)
        s = d.session
        old_hit = s.hit_at

        async def hit_at(x, y):
            if kind == "StatusBar" and (x, y) != target.center:
                s.hit_asks.append((x, y))
                # Make the two horizontal candidates unavailable, exercising vertical candidate clipping.
                return direct if x == target.center[0] else None
            return await old_hit(x, y)

        s.hit_at = hit_at
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            real_dispatch(s)
            await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert len(s.dispatched) == 1
            probes = [p for p in s.hit_asks if p != target.center]
            assert probes and all(not ui._holds(bar.rect, p) for p in probes)
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("recipient", ["address", "nested_action", "foreign", "holder", "unanswered"])
def test_an_unproven_center_recipient_never_probes_background(recipient):
    async def go():
        d, target, private, _, address = navbar(nested=recipient == "nested_action")
        s = d.session
        if recipient == "address":
            s.center_answer = address
        elif recipient == "nested_action":
            s.center_answer = ui.Hit("Button", "post", "Post", private.rect, APP)
        else:
            s.center_answer = weak_hit(recipient, private, private)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            real_dispatch(s)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched and set(s.hit_asks) == {target.center}
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("recipient", ["unanswered", "holder", "foreign", "changed", "other_button"])
def test_background_candidates_must_hit_the_exact_current_button(recipient):
    async def go():
        d, target, private, direct, _ = navbar()
        d.session.background_answer = weak_hit(recipient, direct, private)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched
            assert 1 <= len([p for p in s.hit_asks if p != target.center]) <= 4
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change", ["unanswered", "holder", "foreign", "changed", "private", "disabled", "alert"])
def test_recovered_background_requires_fresh_exact_proof_after_touch_notification(change, monkeypatch):
    async def go():
        d, target, private, direct, _ = navbar()
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            notified = []

            async def notify(ms):
                notified.append(True)
                if change in ("disabled", "alert"):
                    s.source_now = changed_source(s.last_source, change)
                else:
                    s.background_answer = weak_hit(change, direct, private)

            monkeypatch.setattr(d._agent_touches, "notify", notify)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert notified and not s.dispatched
            assert len({p for p in s.hit_asks if p != target.center}) == 1
            assert s.before_input is None
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change", ["changed", "disabled", "alert"])
def test_fresh_source_must_still_prove_the_button_before_any_background_probe(change):
    async def go():
        d, target, _, _, _ = navbar()
        d.session.source_now = changed_source(d.session.last_source, change)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched and set(s.hit_asks) == {target.center}
            assert "read_source" in s.calls
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("lose_final_proof", [False, True])
def test_policy_confirmation_and_its_reread_preserve_background_proof(lose_final_proof, monkeypatch):
    async def go():
        d, target, _, _, _ = navbar(label="Sign out")
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert error.value.code == "not_allowed" and "needs_confirmation" in str(error.value)
            assert not s.dispatched
            [asked] = list(d._confirms.values())
            assert "Sign out" in str(asked.message)
            await d.viewer_message({"type": "confirm_answer", "id": asked.id, "allow": True})

            async def notify(ms):
                if lose_final_proof:
                    s.background_answer = None

            monkeypatch.setattr(d._agent_touches, "notify", notify)
            if lose_final_proof:
                with pytest.raises(Refused):
                    await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
                assert not s.dispatched
            else:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
                assert len(s.dispatched) == 1 and s.hit_asks[-1] != target.center
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("mode", ["double_tap", "long_press", "drafts"])
def test_recovery_keeps_tool_and_drafts_restrictions(mode):
    async def go():
        d, target, _, _, _ = navbar(label="Post" if mode == "drafts" else "Cancel")
        stop, task = await started(d)
        double = daemon_module._DOUBLE.set(mode == "double_tap")
        drafts = daemon_module._DRAFTS_ROUTE.set(mode == "drafts")
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            tool = "long_press" if mode == "long_press" else "tap"
            with pytest.raises(Refused) as error:
                await d.run_tool("a", tool, {"target": {"id": "control"}, "look": False}, policy=NO_CONFIRM)
            assert not s.dispatched
            if mode == "drafts":
                assert error.value.code == "not_allowed" and error.value.draft
            else:
                assert set(s.hit_asks) <= {target.center}
        finally:
            daemon_module._DRAFTS_ROUTE.reset(drafts)
            daemon_module._DOUBLE.reset(double)
            await finish(stop, task)
    run(go)
