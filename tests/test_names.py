"""Names are ASCII, and a name that differs from an existing one only in case is refused (AGENT-TOOLS 1.13.1, item 5)."""
import asyncio
import json

import pytest
from test_daemon import finish, make, started
from test_maptool import ctx
from test_routes import APP, sign_in

from tapwright import batch, compare, mapfile, maptool, names, places
from tapwright.control import Refused


def run(go):
    asyncio.run(go())


# ---- the helper ----

def test_a_twin_is_another_spelling_of_an_existing_name_in_another_case():
    assert names.twin("HOME", ["home", "login"]) == "home"
    assert names.twin("Home", ["HOME"]) == "HOME"
    assert names.twin("home", ["home", "login"]) is None  # the same name is the same file, not a twin
    assert names.twin("home", ["homes", "login"]) is None
    assert names.twin("straße", ["STRASSE"]) is None  # (names are ASCII: no folding of other scripts)


def test_refusing_a_twin_names_the_existing_one_and_says_why():
    with pytest.raises(Refused) as e:
        names.refuse_twin("script", "HOME", ["home"])
    assert e.value.code == "bad_params"
    assert str(e.value) == ('the script "HOME" differs only in case from the existing script "home" (one file on Windows '
                            'and macOS); use "home", or pick another name')
    names.refuse_twin("script", "home", ["home"])  # the same name: nothing to refuse


# ---- scripts ----

@pytest.fixture
def scripts(tmp_path, monkeypatch):
    folder = tmp_path / "scripts"
    folder.mkdir()
    monkeypatch.setattr(batch, "scripts_dir", lambda: folder)
    (folder / "home.json").write_text(json.dumps(batch.script("home", None, [])), "utf-8")
    return folder


def test_a_script_name_with_a_letter_beyond_ascii_is_refused_and_the_file_name_never_has_one(scripts):
    assert batch.named("café menu") == scripts / "caf- menu.json"

    async def go():
        d = make()
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "record", {"start": True, "name": "café"})
        assert e.value.code == "bad_params" and "ASCII" in str(e.value) and "café" in str(e.value)
        await d.run_tool("a", "record", {"start": True, "name": "cafe"})  # an ASCII name is fine
        await finish(stop, task)
    run(go)


def test_a_script_that_differs_from_a_saved_one_only_in_case_is_refused_before_anything_is_written(scripts):
    async def go():
        d = make()
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "record", {"start": True, "name": "HOME"})
        assert e.value.code == "bad_params" and 'existing script "home"' in str(e.value)
        await d.run_tool("a", "record", {"start": True, "name": "tour"})
        with pytest.raises(Refused) as e:  # a path of its own is checked on the folder it names
            await d.run_tool("a", "record", {"stop": True, "path": str(scripts / "Home.json")})
        assert e.value.code == "bad_params" and 'existing script "home.json"' in str(e.value)
        assert [p.name for p in scripts.iterdir()] == ["home.json"]
        r = await d.run_tool("a", "record", {"stop": True})  # still recording: another path works
        assert r["text"].startswith("Saved 0 steps to ") and (scripts / "tour.json").exists()
        await finish(stop, task)
    run(go)


def test_writing_over_a_script_of_the_same_name_is_not_a_twin(scripts):
    assert batch.writable(str(scripts / "home.json")) == scripts / "home.json"
    with pytest.raises(Refused):
        batch.writable(str(scripts / "HOME.json"))


# ---- reference pictures ----

def test_a_reference_name_and_its_bundle_folder_are_ascii():
    for bad in ("café", "aé", "a٠"):
        with pytest.raises(Refused) as e:
            compare.check_params({"name": bad})
        assert e.value.code == "bad_params", bad
    compare.check_params({"name": "home-1.v2_ok"})
    assert compare.reference_path("com.exämple.app", "x").parent.name == "com.ex_mple.app"


def test_a_reference_that_differs_from_a_saved_one_only_in_case_is_refused(tmp_path, monkeypatch):
    refs = tmp_path / "refs"
    monkeypatch.setattr(compare, "references_dir", lambda: refs)
    (refs / "com.example.app").mkdir(parents=True)
    (refs / "com.example.app" / "home.png").write_bytes(b"x")
    assert compare.twin("com.example.app", "HOME") == "home"
    assert compare.twin("com.example.app", "home") is None
    assert compare.twin("com.example.other", "HOME") is None
    own = tmp_path / "t.references"
    own.mkdir()
    (own / "Login.png").write_bytes(b"x")
    assert compare.twin("com.example.app", "login", tmp_path / "t.json") == "Login"


# ---- routes and places ----

def test_a_route_that_differs_from_a_saved_one_only_in_case_is_refused_naming_it(tmp_path):
    file = mapfile.MapFile(tmp_path, APP)
    file.write_route("sign_in", sign_in())
    for twin in ("SIGN_IN", "Sign_In"):
        with pytest.raises(ValueError) as e:
            file.write_route(twin, sign_in())
        assert 'differs only in case from the existing route "sign_in"' in str(e.value)
    with pytest.raises(ValueError) as e:  # no twin: the plain rule
        file.write_route("Other", sign_in())
    assert "lower-case ASCII" in str(e.value)
    assert sorted(p.name for p in file.routes_dir.iterdir()) == ["sign_in.json"]


def test_a_place_named_by_a_person_with_capitals_that_would_be_another_places_name_is_refused():
    data = places.empty_map("com.example.app")
    for name in ("home", "profile"):
        data["places"][name] = {"key": {"signature": name, "parts": {}}, "link": None, "titles": {},
                                "last": "2026-11-12T10:00:00Z"}
    with pytest.raises(ValueError) as e:
        places.set_name(data, "profile", "HOME")
    assert 'differs only in case from the existing place "home"' in str(e.value)
    assert list(data["places"]) == ["home", "profile"]  # nothing moved
    assert places.set_name(data, "profile", "home").name == "home_2"  # the same spelling still gets its number
    assert places.set_name(data, "home", "Home").name == "home"  # a place's own name in another case is only a respelling
    with pytest.raises(Refused) as e:
        maptool.edit(data, ctx(current="home_2"), {"name": "Home"})
    assert e.value.code == "bad_params" and 'existing place "home"' in str(e.value)
