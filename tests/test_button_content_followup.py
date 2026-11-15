"""Synthetic sibling-content regressions; no recorded app hierarchy or phone access.

A sibling remains unrelated. Only a separate live hit on the original button can
authorize an uncovered point, and the real Session path checks it again at POST.
"""
import dataclasses

import pytest

from test_171_button_content import NO_CONFIRM, SHAPES, setup
from test_daemon import finish, run, started
from test_review_17_c1 import APP
from test_review_17_c4_touches import real_dispatch
from test_runner_v5 import node
from tapwright import daemon as daemon_module, ui
from tapwright.control import Refused


def shape(label="Account", content="Account"):
    _, rect, kind, _, content_rect = SHAPES[1]
    return label, rect, kind, content, content_rect


@pytest.mark.parametrize("label,content,placement", [
    ("Account", "Account", "sibling"),
    ("Language, English", "English", "sibling"),
    ("Language, English", "English", "omitted"),
])
def test_passive_content_recovers_only_at_an_exact_button_background(label, content, placement):
    async def go():
        d, target, passive, _ = setup(shape(label, content), placement=placement)
        relation = "other" if placement == "sibling" else "unplaced"
        assert ui.hit_relation(d.session.last_source, target, passive) == relation
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False}, policy=NO_CONFIRM)
            [(_, payload)] = s.dispatched
            point = (payload["x"], payload["y"])
            assert point != target.center and ui._holds(target.rect, point)
            assert not ui._holds(passive.rect, point)
            assert s.hit_asks == [target.center, point, point]
            assert s.calls.count("read_source") == 2
            assert ui.hit_relation(s.last_source, target, passive) == relation
            assert s.before_input is None
        finally:
            await finish(stop, task)
    run(go)


def test_refresh_may_place_previously_omitted_content_as_a_sibling_without_authorizing_it():
    async def go():
        d, target, passive, _ = setup()
        sibling, _, _, _ = setup(placement="sibling")
        d.session.source_now = sibling.session.last_source
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert len(s.dispatched) == 1
            assert ui.hit_relation(s.last_source, target, passive) == "other"
            assert s.hit_asks[0] == target.center and s.hit_asks[-1] != target.center
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change", ["foreign", "unknown", "mismatched", "substring", "name", "interactive"])
def test_sibling_and_composite_words_never_relax_ownership_identity_or_action_guards(change):
    async def go():
        d, target, passive, _ = setup(shape("Language, English", "English"), placement="sibling")
        d.session.center_answer = {
            "foreign": dataclasses.replace(passive, bundle="com.example.foreign"),
            "unknown": dataclasses.replace(passive, bundle=""),
            "mismatched": dataclasses.replace(passive, name="French", label="French"),
            "substring": dataclasses.replace(passive, name="Eng", label="Eng"),
            "name": dataclasses.replace(passive, name="separate-action"),
            "interactive": dataclasses.replace(passive, type="Button"),
        }[change]
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


@pytest.mark.parametrize("answer", ["foreign", "unknown", "changed", "actionable", "holder", "unanswered"])
def test_sibling_recovery_never_accepts_a_different_background_recipient(answer):
    async def go():
        d, target, _, direct = setup(placement="sibling")
        d.session.background_answer = {
            "foreign": dataclasses.replace(direct, bundle="com.example.foreign"),
            "unknown": dataclasses.replace(direct, bundle=""),
            "changed": dataclasses.replace(direct, label="Delete account"),
            "actionable": dataclasses.replace(direct, name="post", label="Post"),
            "holder": ui.Hit("Window", "", "", (0, 0, 430, 932), APP),
            "unanswered": None,
        }[answer]
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched
            assert s.hit_asks[0] == target.center
            assert 2 <= len(s.hit_asks) <= 5
            if answer != "unanswered":
                assert len(s.hit_asks) == 2  # a different recipient ends the search
        finally:
            await finish(stop, task)
    run(go)


def test_matching_sibling_text_inside_a_separate_action_does_not_authorize_that_action():
    async def go():
        d, target, passive, _ = setup(placement="sibling")
        content = node("StaticText", passive.rect, n=passive.name, l=passive.label)
        separate = node("Button", target.rect, [content], n="post", l="Post")
        original = node("Button", target.rect, n=target.name, l=target.label)
        # Tree order cannot prove delivery: the live hit says the separate action is in front.
        source = ui.xml_of_tree(node("Application", (0, 0, 430, 932), [
            node("Window", (0, 0, 430, 932), [separate, original])], b=APP))
        d.session.last_source = source
        d.session.tree = ui.read(source, d.session.screen).items
        d.session.background_answer = ui.Hit("Button", "post", "Post", target.rect, APP)
        assert ui.hit_relation(source, target, passive) == "other"
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched
            assert len(s.hit_asks) == 2 and s.hit_asks[1] != target.center
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("change", ["foreign", "unanswered", "changed", "passive", "disabled", "bad_source"])
def test_sibling_background_proof_must_survive_until_the_actual_post(change, monkeypatch):
    async def go():
        d, _, passive, direct = setup(placement="sibling")
        notified = []
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)

            async def notify(ms):
                notified.append(ms)
                if change == "disabled":
                    s.source_now = s.last_source.replace('enabled="true" x="28"', 'enabled="false" x="28"')
                elif change == "bad_source":
                    s.source_now = "<not-xml"
                else:
                    s.background_answer = {
                        "foreign": dataclasses.replace(direct, bundle="com.example.foreign"),
                        "unanswered": None,
                        "changed": dataclasses.replace(direct, label="Delete account"),
                        "passive": passive,
                    }[change]

            monkeypatch.setattr(d._agent_touches, "notify", notify)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert notified and not s.dispatched
            assert len(set(s.hit_asks[1:])) == 1  # delivery never searches for another point
            assert s.before_input is None
        finally:
            await finish(stop, task)
    run(go)


@pytest.mark.parametrize("cover", ["full_content", "Keyboard", "TabBar"])
def test_sibling_recovery_never_searches_through_full_content_or_hard_covers(cover):
    async def go():
        d, target, passive, _ = setup(placement="sibling")
        if cover == "full_content":
            d.session.center_answer = dataclasses.replace(passive, rect=target.rect)
        else:
            target.covered_by = (ui.Bar(cover, target.rect, "synthetic cover"),)
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused):
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert not s.dispatched
            assert not s.hit_asks or set(s.hit_asks) == {target.center}
        finally:
            await finish(stop, task)
    run(go)


def test_recovered_sibling_signout_still_needs_its_own_confirmation():
    async def go():
        d, target, passive, _ = setup(SHAPES[2], placement="sibling")
        stop, task = await started(d)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            assert error.value.code == "not_allowed" and "needs_confirmation" in str(error.value)
            assert not s.dispatched
            [asked] = list(d._confirms.values())
            await d.viewer_message({"type": "confirm_answer", "id": asked.id, "allow": True})
            await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False})
            [(_, payload)] = s.dispatched
            point = (payload["x"], payload["y"])
            assert point != target.center and not ui._holds(passive.rect, point)
            assert s.hit_asks[-1] == point
        finally:
            await finish(stop, task)
    run(go)


def test_recovered_sibling_post_still_stops_at_the_drafts_only_guard():
    async def go():
        d, target, _, _ = setup(shape("Post", "Post"), placement="sibling")
        stop, task = await started(d)
        token = daemon_module._DRAFTS_ROUTE.set(True)
        try:
            await d.run_tool("a", "look", {"screenshot": False})
            s = real_dispatch(d.session)
            with pytest.raises(Refused) as error:
                await d.run_tool("a", "tap", {"target": {"id": "control"}, "look": False}, policy=NO_CONFIRM)
            assert error.value.code == "not_allowed" and error.value.draft
            assert not s.dispatched
            assert len(s.hit_asks) == 2 and s.hit_asks[1] != target.center
        finally:
            daemon_module._DRAFTS_ROUTE.reset(token)
            await finish(stop, task)
    run(go)
