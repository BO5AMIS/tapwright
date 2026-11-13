"""Links: the URL that reaches a place, kept as scheme and path only, params as variables (AGENT-TOOLS §18.3). No phone."""
import json

import pytest

from tapwright import links, places

NOW = "2026-11-12T10:00:00Z"


def book(**given):
    """A map whose places have these links."""
    data = places.empty_map("com.example.wayfare")
    for i, (name, link) in enumerate(given.items()):
        data["places"][name] = {"key": {"signature": f"s{i}", "parts": {}}, "link": link, "titles": {}, "last": NOW}
    return data


def test_a_link_that_matches_a_known_pattern_is_kept_as_that_pattern():
    data = book(settings="wayfare://settings", trips_id="wayfare://trips/[id]", home="wayfare://")
    assert links.canonical(data, "wayfare://settings") == "wayfare://settings"
    assert links.canonical(data, "wayfare://trips/42") == "wayfare://trips/[id]"
    assert links.canonical(data, "wayfare://trips/42/") == "wayfare://trips/[id]"
    assert links.canonical(data, "WAYFARE://settings") == "wayfare://settings"
    assert links.canonical(data, "wayfare://") == "wayfare://" and links.canonical(data, "wayfare:///") == "wayfare://"


def test_the_query_and_the_fragment_are_dropped_and_never_matter():
    data = book(invite="wayfare://invite/[code]")
    for url in ("wayfare://invite/abc?token=SECRET", "wayfare://invite/abc#SECRET", "wayfare://invite/abc?a=1#b"):
        assert links.canonical(data, url) == "wayfare://invite/[code]"


def test_a_link_with_no_pattern_is_not_saved():
    data = book(settings="wayfare://settings")
    for url in ("wayfare://invite/abc?token=SECRET", "wayfare://settings/extra", "wayfare://trips/42", "https://example.com/x",
                "settings", "", "wayfare:settings"):
        assert links.canonical(data, url) is None
    assert links.canonical(book(), "wayfare://settings") is None  # nothing is known yet: every segment may hold a value


def test_a_param_takes_one_segment_that_is_not_empty():
    data = book(trips_id="wayfare://trips/[id]")
    assert links.canonical(data, "wayfare://trips//") is None and links.canonical(data, "wayfare://trips/1/2") is None


def test_a_place_reached_by_a_link_remembers_it():
    data = book(trips_id="wayfare://trips/[id]", detail=None)
    done = links.landed(data, "wayfare://trips/42?utm=x", "detail")
    assert done.link == "wayfare://trips/[id]" and done.note is None
    assert data["places"]["detail"]["link"] == "wayfare://trips/[id]"
    # the map's random title key (hex) can hold "42" by chance: only what was saved for places is checked
    saved = json.dumps(data["places"])
    assert "42" not in saved and "utm" not in saved


def test_a_place_that_has_a_link_keeps_it():
    data = book(settings="wayfare://settings", other="wayfare://other")
    done = links.landed(data, "wayfare://other", "settings")
    assert done.link is None and done.note is None and data["places"]["settings"]["link"] == "wayfare://settings"


def test_a_link_that_matches_nothing_says_so_and_saves_nothing():
    data = book(home=None)
    done = links.landed(data, "wayfare://invite/abc?token=SECRET", "home")
    assert done.link is None
    assert done.note == "Link not saved: it may hold a value; add it with map link."
    assert data["places"]["home"]["link"] is None and "SECRET" not in json.dumps(data)


def test_a_link_by_hand_is_kept_as_given():
    data = book(settings=None)
    assert links.set_link(data, "settings", "wayfare://settings") == "wayfare://settings"
    assert links.set_link(data, "settings", "wayfare://settings/[tab]") == "wayfare://settings/[tab]"
    assert data["places"]["settings"]["link"] == "wayfare://settings/[tab]"
    assert links.set_link(data, "settings", None) is None and data["places"]["settings"]["link"] is None


def test_a_link_by_hand_must_be_a_link_to_a_place_that_exists():
    data = book(settings=None)
    for bad in ("settings", "", "wayfare:", "://x", " ", "wayfare://a b"):
        with pytest.raises(ValueError):
            links.set_link(data, "settings", bad)
    with pytest.raises(KeyError):
        links.set_link(data, "nowhere", "wayfare://x")


def test_a_pattern_names_its_params_and_takes_values():
    assert links.variables("wayfare://trips/[id]/photos/[n]") == ["id", "n"] and links.variables("wayfare://settings") == []
    assert links.fill("wayfare://trips/[id]", {"id": "42"}) == "wayfare://trips/42"
    assert links.fill("wayfare://trips/[id]", {"id": "a b/c"}) == "wayfare://trips/a%20b%2Fc"
    assert links.fill("wayfare://settings", {}) == "wayfare://settings"
    with pytest.raises(ValueError, match="needs id"):
        links.fill("wayfare://trips/[id]", {})


def test_a_percent_encoded_link_is_matched_and_kept_by_what_it_says():
    """1.6 security (Tapline's finding): `wayfare://trips/%34%38%32` is the id 482 and `%74rips` is `trips`. Segments are
    compared decoded (again until stable); what is kept is the place's pattern, never the opened text."""
    data = book(trips_id="wayfare://trips/[id]", settings="wayfare://settings")
    for url in ("wayfare://%74rips/%34%38%32", "wayfare://trips/%2534%2538%2532", "wayfare://trips/%34%38%32?t=%53ECRET"):
        kept = links.canonical(data, url)
        assert kept == "wayfare://trips/[id]" and "482" not in kept and "%" not in kept
    assert links.canonical(data, "wayfare://%73ettings") == "wayfare://settings"
    assert links.canonical(data, "wayfare://settings/%34%38%32") is None  # an id after a fixed place: not a known link
    assert links.canonical(data, "wayfare://trips/%2F") == "wayfare://trips/[id]"  # %2F is text in one segment, not a slash
    assert links.canonical(data, "wayfare://trips/" + "%25" * 40 + "34") == "wayfare://trips/[id]"  # (bounded decoding)
    data = book(trips_id="wayfare://trips/[id]", detail=None)
    done = links.landed(data, "wayfare://trips/%34%38%32", "detail")
    assert done.link == "wayfare://trips/[id]" and data["places"]["detail"]["link"] == "wayfare://trips/[id]"


def test_a_segment_that_cannot_be_read_for_sure_matches_nothing():
    """Tapline's Codex finding: an escape left after the last decoding round, or one that isn't UTF-8, fails closed."""
    from tapwright.links import _decoded
    assert _decoded("%34%38%32") == "482" and _decoded("plain") == "plain"
    assert _decoded("%FF%64") is None  # not UTF-8
    deep = "4"
    for _ in range(10):
        deep = deep.replace("%", "%25") if "%" in deep else "%34"
    assert _decoded(deep) is None  # still encoded after the bounded rounds
