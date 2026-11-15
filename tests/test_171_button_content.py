"""Private button content may be absent from XCTest's app tree: recover only by hitting the button itself.

The report's geometry is synthetic test data, not a recording of the JLD accessibility hierarchy.
"""
import dataclasses

import pytest

from test_daemon import finish, make, run, started
from test_review_17_c1 import APP
from test_review_17_c4_touches import real_dispatch
from test_runner_v5 import node
from tapwright import daemon as daemon_module, policy, ui
from tapwright.control import Refused


SHAPES = [
    ("Settings", (370, 98, 44, 45), "Other", "", (381, 109, 22, 23)),
    ("Account", (28, 594, 374, 49), "StaticText", "Account", (76, 607, 296, 24)),
    ("Sign out", (28, 246, 374, 45), "StaticText", "Sign out", (186, 256, 58, 24)),
]
NO_CONFIRM = policy.parse({"version": 1, "confirm": [], "block": []})


def setup(shape=SHAPES[1], *, placement="omitted", kind="Button", enabled=True):
    label, rect, content_type, content_label, content_rect = shape
    content = node(content_type, content_rect, n=content_label, l=content_label)
    control = node(kind, rect, [content] if placement == "inside" else (), n="control", l=label, d=not enabled)
    siblings = [control, content] if placement == "sibling" else [control]
    source = ui.xml_of_tree(node("Application", (0, 0, 430, 932), [
        node("Window", (0, 0, 430, 932), siblings)], n="Example", b=APP))
    d = make()
    s = d.session
    s.screen = (430, 932)
    s.tree = ui.read(source, s.screen).items
    s.last_source = source
    target = next(e for e in s.tree if e.name == "control")
    inside = ui.Hit(content_type, content_label, content_label, content_rect, APP)
    direct = ui.Hit(kind, "control", label, rect, APP)

    async def hit_at(x, y):
        s.hit_asks.append((x, y))
        return s.center_answer if (x, y) == target.center else s.background_answer

    s.hit_at, s.center_answer, s.background_answer = hit_at, inside, direct
    return d, target, inside, direct


@pytest.mark.parametrize("shape", SHAPES, ids=[s[0] for s in SHAPES])
def test_private_content_uses_only_a_proven_button_background_and_dispatches_that_point(shape):
    async def go():
        d, target, inside, _ = setup(shape)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            result = await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False}, policy=NO_CONFIRM)
            [(_, payload)] = s.dispatched
            point = (payload["x"], payload["y"])
            assert result["text"].startswith("Tapped")
            assert point != target.center and not ui._holds(inside.rect, point)
            assert ui._holds(target.rect, point)
            assert s.hit_asks == [target.center, point, point]  # final POST revalidates this exact point
            assert s.calls.count("read_source") == 2  # matching enabled button proved at selection and dispatch
            assert payload.get("x") and payload.get("y")
            assert s.before_input is None
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("placement", ["inside", "sibling"])
def test_known_descendant_still_uses_center_and_known_overlay_never_searches(placement):
    async def go():
        d, target, _, _ = setup(placement=placement)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            if placement == "inside":
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
                assert len(s.dispatched) == 1
            else:
                with pytest.raises(Refused) as error:
                    await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
                assert error.value.code == "not_actionable" and not s.dispatched
            assert set(s.hit_asks) == {target.center}
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change", ["foreign", "unknown_app", "actionable", "image", "different_words", "changed"])
def test_disallowed_center_hits_never_probe_background(change):
    async def go():
        d, target, inside, direct = setup()
        alternatives = {
            "foreign": dataclasses.replace(inside, bundle="com.example.foreign"),
            "unknown_app": dataclasses.replace(inside, bundle=""),
            "actionable": dataclasses.replace(inside, type="Button", name="Post", label="Post"),
            "image": dataclasses.replace(inside, type="Image"),
            "different_words": dataclasses.replace(inside, name="Delete", label="Delete"),
            "changed": dataclasses.replace(direct, label="Delete account"),
        }
        d.session.center_answer = alternatives[change]
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert error.value.code == ("stale" if change == "changed" else "not_actionable")
            assert s.hit_asks == [target.center] and not s.dispatched
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change", ["unanswered", "holder", "foreign", "changed", "other_button"])
def test_a_background_probe_never_accepts_weaker_proof(change):
    async def go():
        d, _, _, direct = setup()
        d.session.background_answer = {
            "unanswered": None,
            "holder": ui.Hit("Window", "", "", (0, 0, 430, 932), APP),
            "foreign": dataclasses.replace(direct, bundle="com.example.foreign"),
            "changed": dataclasses.replace(direct, label="Delete account"),
            "other_button": dataclasses.replace(direct, name="post", label="Post"),
        }[change]
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched and 2 <= len(s.hit_asks) <= 5
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change", ["unanswered", "holder", "foreign", "changed", "inside", "disabled", "alert", "read_failed"])
def test_recovered_point_is_checked_again_after_touch_notification_before_actual_post(change, monkeypatch):
    async def go():
        d, target, inside, direct = setup()
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)

            async def notify(ms):
                if change == "disabled":
                    s.source_now = s.last_source.replace('enabled="true" x="28"', 'enabled="false" x="28"')
                elif change == "alert":
                    s.source_now = s.last_source.replace("</XCUIElementTypeWindow>",
                        '<XCUIElementTypeAlert type="XCUIElementTypeAlert" x="50" y="500" width="300" height="100"/>'
                        '</XCUIElementTypeWindow>')
                elif change == "read_failed":
                    async def failed():
                        raise RuntimeError("synthetic read unavailable")
                    s.read_source = failed
                else:
                    s.background_answer = {
                        "unanswered": None,
                        "holder": ui.Hit("Window", "", "", (0, 0, 430, 932), APP),
                        "foreign": dataclasses.replace(direct, bundle="com.example.foreign"),
                        "changed": dataclasses.replace(direct, label="Delete account"),
                        "inside": inside,
                    }[change]

            monkeypatch.setattr(d._agent_touches, "notify", notify)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched
            assert len({p for p in s.hit_asks if p != target.center}) == 1  # never relocate again at delivery
            assert s.before_input is None
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("mode", ["confirmation", "drafts", "disabled", "long_press"])
def test_recovery_preserves_confirmation_drafts_disabled_and_tool_restrictions(mode):
    async def go():
        d, target, _, _ = setup(SHAPES[2], enabled=mode != "disabled")
        stop, task = await started(d)
        draft_token = daemon_module._DRAFTS_ROUTE.set(mode == "drafts")
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            tool = "long_press" if mode == "long_press" else "tap"
            with pytest.raises(Refused) as error:
                await d.run_tool("a", tool, {"target": {"id": "control"}, "look": False})
            assert not s.dispatched
            if mode == "confirmation":
                assert error.value.code == "not_allowed" and "needs_confirmation" in str(error.value)
            elif mode == "disabled":
                assert str(error.value).startswith("disabled:") and not s.hit_asks
            elif mode == "long_press":
                assert s.hit_asks == [target.center]
        finally:
            daemon_module._DRAFTS_ROUTE.reset(draft_token)
            await finish(stop, task)
    run(go)


def test_recovered_signout_still_waits_for_its_own_confirmation_before_dispatch():
    async def go():
        d, target, inside, _ = setup(SHAPES[2])
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert error.value.code == "not_allowed" and not s.dispatched
            [asked] = list(d._confirms.values())
            assert "Sign out" in str(asked.message)
            await d.viewer_message({"type": "confirm_answer", "id": asked.id, "allow": True})
            await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            [(_, payload)] = s.dispatched
            point = (payload["x"], payload["y"])
            assert point != target.center and not ui._holds(inside.rect, point)
            assert s.hit_asks[-1] == point
        finally:
            await finish(stop, task)
    run(go)


def test_recovered_post_still_stops_a_drafts_only_route_even_without_policy_words():
    async def go():
        _, rect, kind, _, content_rect = SHAPES[1]
        d, _, _, _ = setup(("Post", rect, kind, "Post", content_rect))
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False}, policy=NO_CONFIRM)
            assert error.value.code == "not_allowed" and error.value.draft
            assert not s.dispatched
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("condition", ["changed", "disabled", "missing_app", "duplicate", "malformed", "gone"])
def test_refresh_must_prove_current_unique_enabled_button_before_any_probe(condition):
    async def go():
        d, target, _, _ = setup()
        original = d.session.last_source
        button_line = next(line for line in original.splitlines() if 'name="control"' in line)
        d.session.source_now = {
            "changed": original.replace('label="Account"', 'label="Delete account"'),
            "disabled": original.replace('enabled="true" x="28"', 'enabled="false" x="28"'),
            "missing_app": original.replace(f' bundleId="{APP}"', ""),
            "duplicate": original.replace(button_line, button_line + "\n" + button_line),
            "malformed": "<not-xml",
            "gone": original.replace(button_line, ""),
        }[condition]
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert s.hit_asks == [target.center] and not s.dispatched
        finally:
            await finish(stop, task)
    run(go)


def test_background_candidates_stay_in_visible_inset_regions_and_need_room():
    d, target, inside, _ = setup()
    target.covered_by = (ui.Bar("Keyboard", (0, 620, 430, 312), "keyboard"),)
    points = ui.button_background_points(d.session.last_source, target, inside, APP, d.session.screen)
    assert points and len(points) <= 4
    assert all(target.x + 3 <= x <= target.x + target.width - 3 and target.y + 3 <= y < 620 for x, y in points)
    assert all(not ui._holds(inside.rect, point) for point in points)
    full = dataclasses.replace(inside, rect=target.rect)
    assert ui.button_background_points(d.session.last_source, target, full, APP, d.session.screen) == []


@pytest.mark.parametrize("kind", ["Cell", "Other", "TextField", "Switch"])
def test_only_native_buttons_qualify_for_background_recovery(kind):
    d, target, inside, _ = setup(kind=kind)
    assert ui.button_background_points(d.session.last_source, target, inside, APP, d.session.screen) == []


def test_double_tap_stress_never_recovers_an_unplaced_button_background():
    """The second stress touch is a raw repeat; it cannot inherit a single-touch recovery proof."""
    async def go():
        d, target, _, _ = setup()
        stop, task = await started(d)
        token = daemon_module._DOUBLE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert error.value.code == "not_actionable"
            assert s.hit_asks == [target.center] and not s.dispatched
        finally:
            daemon_module._DOUBLE.reset(token)
            await finish(stop, task)
    run(go)
