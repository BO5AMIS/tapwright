"""The `map` tool's answers and edits (AGENT-TOOLS §18.8). No phone."""
import pytest

from tapwright import maptool, places, policy, roads
from tapwright.control import Refused

NOW = "2026-11-12T10:00:00Z"
BUILD = "1.5.0 (220)"


def place(sig, link=None, last=NOW, **kw):
    return {"key": {"signature": sig, "parts": {}}, "link": link, "titles": {}, "last": last, **kw}


def road(src, dst, target, build=BUILD):
    return {"from": src, "to": dst, "move": {"tool": "tap", "target": {"id": target}}, "names": {}, "by": "tapwright",
            "seen": 1, "failed": 0, "ms": 100, "last": NOW, "build": build}


def wayfare():
    data = places.empty_map("com.example.wayfare")
    data["places"] = {"home": place("h", "wayfare://"), "settings": place("s", "wayfare://settings"), "language": place("l"),
                      "profile": place("p"), "trips_id": place("t", "wayfare://trips/[id]")}
    data["roads"] = [road("home", "settings", "s"), road("settings", "language", "l"), road("home", "profile", "p", "1.4.0 (1)")]
    return data


def ctx(**kw):
    base = dict(app="com.example.wayfare", name="Wayfare", build=BUILD, current="home", accounts=(), routes=[], edges=[],
                policy=policy.DEFAULT)
    return maptool.Context(**{**base, **kw})


def test_the_listing_as_the_spec_shows_it():
    data = wayfare()
    data["routes"] = {"sign_in": "routes/sign_in.json", "switch_language": "routes/switch_language.json"}
    data["reflexes"] = [{"name": "rate_prompt", "when": {"text": "Rate Wayfare"}, "do": {"tool": "tap", "target": {"text": "Not now"}}}]
    lines = maptool.listing(data, ctx(routes=[("sign_in", "welcome", "home", ["account"]),
                                              ("switch_language", "settings", "language", ["language"])],
                                      accounts=("tester1", "tester2")))
    assert lines == [
        "Map of Wayfare (com.example.wayfare), build 1.5.0 (220): 5 places, 3 roads (2 proven), 2 routes, 1 reflex. Now at: home.",
        "Places: home (link wayfare://), settings (link wayfare://settings), language, profile, trips_id (link wayfare://trips/[id])",
        "Routes: sign_in (welcome → home; account), switch_language (settings → language; language)",
        "Reflexes: rate_prompt.",
        "Accounts: tester1, tester2."]


def test_the_listing_leaves_out_what_there_is_none_of_and_what_it_does_not_know():
    data = wayfare()
    lines = maptool.listing(data, ctx(name=None, build=None, current=None))
    assert lines[0] == "Map of com.example.wayfare: 5 places, 3 roads (3 proven), 0 routes, 0 reflexes. Now at: unknown."
    assert len(lines) == 2 and lines[1].startswith("Places: home")
    one = places.empty_map("com.example.wayfare")
    one["places"] = {"home": place("h")}
    one["roads"] = [road("home", "x", "g")]
    assert maptool.listing(one, ctx(current="home"))[0] == \
        "Map of Wayfare (com.example.wayfare), build 1.5.0 (220): 1 place, 1 road (1 proven), 0 routes, 0 reflexes. Now at: home."


def test_an_empty_map_says_so():
    lines = maptool.listing(places.empty_map("com.example.wayfare"), ctx(current=None))
    assert lines == ["Map of Wayfare (com.example.wayfare), build 1.5.0 (220): no places yet. Look or act, and what is seen is saved."]


def test_at_most_sixty_places_then_an_ellipsis():
    data = places.empty_map("com.example.wayfare")
    data["places"] = {f"p{n}": place(f"s{n}") for n in range(75)}
    line = maptool.listing(data, ctx())[1]
    assert line.count(",") == 60 and line.endswith(", …") and "p59" in line and "p60" not in line


def test_the_way_is_said_and_a_missing_one_is_not_found():
    data = wayfare()
    assert maptool.way(data, ctx(), "language") == "Would drive to language by 2 taps: home > settings > language (all proven)."
    assert maptool.way(data, ctx(current="language"), "language") == "Already at language."
    assert maptool.way(data, ctx(), "trips_id") == "Would drive to trips_id by link: wayfare://trips/[id]."
    assert maptool.way(data, ctx(), "profile") == "Would drive to profile by 1 tap: home > profile (1 of 1 roads unproven for this build)."
    with pytest.raises(Refused) as e:
        maptool.way(data, ctx(current="language"), "profile")
    assert e.value.code == "not_found" and "no known way from language to profile; drive it once" in str(e.value)
    with pytest.raises(Refused) as e:
        maptool.way(data, ctx(), "nowhere")
    assert e.value.code == "not_found" and "no place called nowhere" in str(e.value) and "home" in str(e.value)
    with pytest.raises(Refused) as e:
        maptool.way(data, ctx(current=None), "language")
    assert e.value.code == "not_found" and "isn't a place the map knows yet" in str(e.value)


def test_a_route_is_a_way_too():
    data = wayfare()
    edges = [roads.RouteEdge("switch_language", "profile", "language", 3)]
    say = maptool.way(data, ctx(current="profile", edges=edges), "language")
    assert "route switch_language" in say


def test_exactly_one_thing_per_call():
    assert maptool.kind({}) == "list"
    assert maptool.kind({"to": "x"}) == "to" and maptool.kind({"name": "x"}) == "name"
    assert maptool.kind({"link": "wayfare://x", "place": "x"}) == "link" and maptool.kind({"forget": "x"}) == "forget"
    for bad in ({"to": "x", "name": "y"}, {"place": "x"}, {"link": 3}, {"to": ""}, {"name": 5}, {"forget_reflex": ["x"]},
                {"reflex": "text"}):
        with pytest.raises(Refused) as e:
            maptool.kind(bad)
        assert e.value.code == "bad_params"


def test_naming_the_current_place():
    data = wayfare()
    assert maptool.edit(data, ctx(current="language"), {"name": "Language Picker"}) == "Named: language_picker."
    assert "language_picker" in data["places"] and data["roads"][1]["to"] == "language_picker"
    assert maptool.edit(data, ctx(current="profile"), {"name": "home"}) == "Named: home_2 (home was taken)."
    with pytest.raises(Refused) as e:
        maptool.edit(data, ctx(current=None), {"name": "x"})
    assert e.value.code == "not_found"
    with pytest.raises(Refused) as e:
        maptool.edit(data, ctx(current="home"), {"name": "اسم"})
    assert e.value.code == "bad_params" and "ASCII" in str(e.value)


def test_a_link_by_hand():
    data = wayfare()
    assert maptool.edit(data, ctx(), {"link": "wayfare://x", "place": "language"}) == "Link for language: wayfare://x."
    assert data["places"]["language"]["link"] == "wayfare://x"
    assert maptool.edit(data, ctx(current="profile"), {"link": "wayfare://profile"}) == "Link for profile: wayfare://profile."
    with pytest.raises(Refused) as e:
        maptool.edit(data, ctx(), {"link": "wayfare://x", "place": "nowhere"})
    assert e.value.code == "not_found"
    with pytest.raises(Refused) as e:
        maptool.edit(data, ctx(), {"link": "not a link", "place": "home"})
    assert e.value.code == "bad_params"
    with pytest.raises(Refused) as e:
        maptool.edit(data, ctx(current=None), {"link": "wayfare://x"})
    assert e.value.code == "not_found"


def test_forgetting_a_place_takes_its_roads():
    data = wayfare()
    data["reflexes"] = [{"name": "r", "when": {"text": "x"}, "do": {"tool": "tap", "target": {"text": "y"}}, "at": "settings"}]
    assert maptool.edit(data, ctx(), {"forget": "settings"}) == "Forgot settings, its 2 roads and 1 reflex."
    assert "settings" not in data["places"] and [r["to"] for r in data["roads"]] == ["profile"] and data["reflexes"] == []
    assert maptool.edit(data, ctx(), {"forget": "profile"}) == "Forgot profile and its 1 road."
    assert maptool.edit(data, ctx(), {"forget": "trips_id"}) == "Forgot trips_id."
    with pytest.raises(Refused) as e:
        maptool.edit(data, ctx(), {"forget": "settings"})
    assert e.value.code == "not_found"


RATE = {"name": "rate_prompt", "when": {"text": "Rate Wayfare"}, "do": {"tool": "tap", "target": {"text": "Not now"}}}


def test_a_reflex_is_added_checked_and_removed():
    data = wayfare()
    assert maptool.edit(data, ctx(), {"reflex": RATE}) == "Reflex added: rate_prompt."
    assert data["reflexes"] == [{**RATE, "max": 1}]
    assert maptool.edit(data, ctx(), {"reflex": {**RATE, "max": 3, "at": "home"}}) == "Reflex updated: rate_prompt."
    assert data["reflexes"][0]["max"] == 3 and data["reflexes"][0]["at"] == "home" and len(data["reflexes"]) == 1
    assert maptool.edit(data, ctx(), {"forget_reflex": "rate_prompt"}) == "Reflex removed: rate_prompt."
    with pytest.raises(Refused) as e:
        maptool.edit(data, ctx(), {"forget_reflex": "rate_prompt"})
    assert e.value.code == "not_found"


@pytest.mark.parametrize("bad", [
    {**RATE, "name": ""}, {**RATE, "name": "Ra te"}, {"name": "x", "do": RATE["do"]}, {"name": "x", "when": RATE["when"]},
    {**RATE, "when": {"text": "a", "id": "b"}}, {**RATE, "when": {"color": "red"}}, {**RATE, "when": {"text": ""}},
    {**RATE, "do": {"tool": "tap"}}, {**RATE, "do": {"tool": "type_text", "text": "x"}},
    {**RATE, "do": {"tool": "open_app", "app": "com.x"}}, {**RATE, "do": {"tool": "tap", "target": {"ref": "e3"}}},
    {**RATE, "max": 0}, {**RATE, "max": 4}, {**RATE, "max": "2"}, {**RATE, "at": "nowhere"}, {**RATE, "extra": 1},
    {**RATE, "do": [RATE["do"], RATE["do"]]}])
def test_a_reflex_that_is_not_one_is_refused(bad):
    with pytest.raises(Refused) as e:
        maptool.edit(wayfare(), ctx(), {"reflex": bad})
    assert e.value.code == "bad_params"


def test_a_reflex_the_policy_flags_is_refused_when_it_is_added():
    data = wayfare()
    for do in ({"tool": "tap", "target": {"text": "Delete"}}, {"tool": "tap", "target": {"label": "Buy now"}},
               {"tool": "tap", "target": {"id": "Delete"}}):
        with pytest.raises(Refused) as e:
            maptool.edit(data, ctx(), {"reflex": {**RATE, "do": do}})
        assert e.value.code == "not_allowed" and "policy" in str(e.value)
    blocked = policy.parse({"block": ["Wipe"]})
    with pytest.raises(Refused) as e:
        maptool.edit(data, ctx(policy=blocked), {"reflex": {**RATE, "do": {"tool": "tap", "target": {"text": "Wipe all"}}}})
    assert e.value.code == "not_allowed"
    assert data["reflexes"] == []


def test_the_apps_name_in_the_listing_is_the_apps_not_a_places():
    # a place's name is ASCII and never from content: the listing prints only names, links and counts
    data = wayfare()
    text = "\n".join(maptool.listing(data, ctx()))
    assert text.isascii() or "→" in text or "…" in text
