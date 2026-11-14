"""Routes (AGENT-TOOLS §18.5): script version 2, its variables and `${...}` values, and the file in the map's folder."""
import json

import pytest

from tapwright import batch, mapfile, places, routes
from tapwright.control import Refused

APP = "com.example.wayfare"
STEPS = [{"tool": "tap", "target": {"text": "Continue"}},
         {"tool": "type_text", "target": {"id": "email"}, "text": "${account.username}"},
         {"tool": "type_text", "target": {"id": "password"}, "text": "${account.password}", "secure": True},
         {"tool": "tap", "target": {"text": "Sign in"}},
         {"tool": "tap", "target": {"text": "Not now"}, "optional": True},
         {"tool": "wait_for", "text": "Home"}]


def sign_in(**over):
    return {**routes.route("sign_in", APP, "welcome", "home", ["account"], STEPS), **over}


def test_a_route_is_a_version_2_script_with_a_start_an_end_and_variables():
    r = sign_in()
    assert (r["format"], r["version"], r["app"], r["name"], r["start"], r["end"], r["variables"]) == \
        ("agent-tools-script", 2, APP, "sign_in", "welcome", "home", ["account"])
    assert r["steps"] == STEPS and "fresh" not in r
    routes.check(r)


def test_a_route_is_checked():
    for bad, why in ((sign_in(start=None), "start"), (sign_in(start=3), "start"), (sign_in(end=3), "end"),
                     (sign_in(variables="account"), "variables"), (sign_in(variables=["a b"]), "variable"),
                     (sign_in(name="Bad Name"), "name")):
        with pytest.raises(Refused) as e:
            routes.check(bad)
        assert e.value.code == "bad_params" and why in str(e.value), bad


def test_an_account_placeholder_needs_account_declared_and_any_other_undeclared_name_is_literal():
    with pytest.raises(Refused) as e:
        routes.check(sign_in(variables=[]))
    assert "account" in str(e.value) and "variables" in str(e.value)
    routes.check(sign_in(steps=[{"tool": "type_text", "text": "${language}"}], variables=["language"]))
    # (Codex, PR #10 cycle 15: an undeclared `${other}` is literal text in typed text too, as in every other field)
    routes.check(sign_in(steps=[{"tool": "type_text", "text": "${other}"}], variables=["language"]))
    assert routes.substitute([{"tool": "type_text", "text": "${other}"}], {"other": "x"}, ["language"])[0]["text"] == "${other}"


def test_an_account_value_is_the_whole_text_of_a_step_never_part_of_one():
    with pytest.raises(Refused) as e:
        routes.check(sign_in(steps=[{"tool": "type_text", "text": "hi ${account.password}"}]))
    assert e.value.code == "bad_params" and "whole text" in str(e.value)
    with pytest.raises(Refused) as e:
        routes.check(sign_in(steps=[{"tool": "tap", "target": {"text": "${account.username}"}}]))
    assert "whole text" in str(e.value) or "type_text" in str(e.value)
    with pytest.raises(Refused):
        routes.check(sign_in(steps=[{"tool": "type_text", "text": "${account.token}"}]))


def test_variables_are_filled_in_everywhere_a_step_has_text():
    steps = [{"tool": "tap", "target": {"text": "${language}"}}, {"tool": "type_text", "text": "to ${language}!"},
             {"tool": "open_url", "url": "wayfare://trips/${id}"}, {"tool": "tap", "target": {"id": "x"}, "expect": {"text": "${language}"}}]
    got = routes.substitute(steps, {"language": "Arabic", "id": "42"})
    assert got[0]["target"] == {"text": "Arabic"} and got[1]["text"] == "to Arabic!" and got[2]["url"] == "wayfare://trips/42"
    assert got[3]["expect"] == {"text": "Arabic"} and steps[0]["target"] == {"text": "${language}"}  # the route itself is untouched


def test_an_undeclared_variable_is_literal_text_in_every_field():
    steps = [{"tool": "tap", "target": {"text": "a ${x} b $${language}"}}]
    got = routes.substitute(steps, {"language": "Arabic", "x": "X"}, ["language"])
    assert got[0]["target"] == {"text": "a ${x} b ${language}"}  # (`x` is not declared; `$${` is a literal `${`)
    routes.check(sign_in(steps=steps, variables=["language"]))


def test_the_recorder_writes_a_literal_dollar_brace_twice_in_every_field_but_the_typed_text_it_writes_itself():
    step = {"tool": "tap", "target": {"label": "hi ${a}"}, "expect": {"text": "ok $${b}"}}
    saved = routes.escaped(step)
    assert saved == {"tool": "tap", "target": {"label": "hi $${a}"}, "expect": {"text": "ok $$${b}"}}
    assert routes.substitute([saved], {}, [])[0] == step  # (replay reads the exact text back)


def test_an_account_placeholder_is_left_for_the_typing_to_fill():
    got = routes.substitute(STEPS, {})
    assert got[1]["text"] == "${account.username}" and got[2]["text"] == "${account.password}"


def test_a_literal_dollar_brace_is_written_twice():
    assert routes.literal("price ${x} and $${y}") == "price $${x} and $$${y}"
    got = routes.substitute([{"tool": "type_text", "text": "cost $${5}"}], {})
    assert got[0]["text"] == "cost ${5}"
    routes.check(sign_in(steps=[{"tool": "type_text", "text": "cost $${5}"}], variables=[]))


def test_a_variable_with_no_value_is_asked_for():
    with pytest.raises(Refused) as e:
        routes.need(sign_in(variables=["language"], steps=[{"tool": "tap", "target": {"text": "${language}"}}]), {}, None)
    assert e.value.code == "bad_params" and "language" in str(e.value) and "with" in str(e.value)
    routes.need(sign_in(variables=["language"], steps=[{"tool": "tap", "target": {"text": "${language}"}}]), {"language": "x"}, None)
    with pytest.raises(Refused) as e:
        routes.need(sign_in(), {}, None)
    assert "account" in str(e.value)
    routes.need(sign_in(), {}, "tester2")


def test_a_recorded_text_becomes_a_variable_and_a_plain_one_is_escaped():
    assert routes.recorded_text("Arabic", "language") == "${language}"
    assert routes.recorded_text("a ${b}", None) == "a $${b}"
    assert routes.recorded_text("plain", None) == "plain"
    with pytest.raises(Refused):
        routes.recorded_text("x", "bad name")
    with pytest.raises(Refused):
        routes.recorded_text("x", "account")


def test_load_script_reads_version_2_and_still_reads_version_1(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(sign_in()), "utf-8")
    assert batch.load_script(str(path))["version"] == 2
    v1 = tmp_path / "v1.json"
    v1.write_text(json.dumps(batch.script("x", APP, [{"tool": "tap", "target": {"id": "a"}}])), "utf-8")
    assert batch.load_script(str(v1))["version"] == 1
    path.write_text(json.dumps(sign_in(version=3)), "utf-8")
    with pytest.raises(Refused) as e:
        batch.load_script(str(path))
    assert "version 3" in str(e.value)


def test_a_version_1_script_keeps_its_dollar_braces_as_they_are():
    steps = [{"tool": "type_text", "text": "${x}"}]
    batch.check_steps(steps, script=True)  # (no variables there: version 1 never changes)


def test_the_route_file_lives_in_the_maps_folder_and_the_map_lists_it(tmp_path):
    file = mapfile.MapFile(tmp_path, APP)
    assert file.write_route("sign_in", sign_in()) == file.routes_dir / "sign_in.json"
    assert json.loads((file.routes_dir / "sign_in.json").read_text("utf-8"))["start"] == "welcome"
    data = file.load()
    assert data["routes"] == {"sign_in": "routes/sign_in.json"}
    assert file.read_route(data, "sign_in")["end"] == "home"
    assert file.route_info(data) == [("sign_in", "welcome", "home", ["account"])]
    edges = file.route_edges(data)
    assert [(e.name, e.start, e.end, e.steps) for e in edges] == [("sign_in", "welcome", "home", 6)]
    assert file.read_route(data, "nope") is None


def test_a_route_name_is_ascii(tmp_path):
    file = mapfile.MapFile(tmp_path, APP)
    for bad in ("Sign In", "../x", "", "a" * 61):
        with pytest.raises(ValueError):
            file.write_route(bad, sign_in())


def test_writing_a_route_keeps_the_places_already_in_the_map(tmp_path):
    file = mapfile.MapFile(tmp_path, APP)
    data = places.empty_map(APP)
    data["places"]["home"] = {"key": {"signature": "a", "parts": {}}, "link": None, "titles": {}, "last": "2026-11-12T10:00:00Z"}
    file.save(data)
    file.write_route("sign_in", sign_in())
    again = file.load()
    assert list(again["places"]) == ["home"] and list(again["routes"]) == ["sign_in"]
