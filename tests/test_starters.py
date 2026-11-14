"""Starter maps and routes (AGENT-TOOLS §24.1): shipping, installing without overwriting, and what the shipped files hold."""
import json
import re
from pathlib import Path

from tapwright import mapfile, starters

SHIPPED = starters.SHIPPED


def _files(folder: Path):
    return [p for p in folder.rglob("*") if p.is_file()]


def test_every_shipped_starter_reads_and_its_routes_name_its_places():
    found = starters.apps()
    assert "com.apple.mobilenotes" in found
    for app in found:
        data, routes = starters.load(app)
        assert data["app"] == app and routes and set(routes) == set(data["routes"])
        for name, route in routes.items():
            assert route["app"] == app and route["name"] == name and route["version"] == 2
            assert route["start"] in data["places"] and route["end"] in data["places"]
            assert all(p["titles"] == {} for p in data["places"].values())
            assert "titleKey" not in data  # a map made from it gets its own random key


def test_install_into_an_empty_maps_folder(tmp_path):
    text, code = starters.install(str(tmp_path))
    assert code == 0 and "com.apple.mobilenotes: added add_note" in text
    mf = mapfile.MapFile(tmp_path, "com.apple.mobilenotes")
    data, problem = mf.read()
    assert problem is None and data["routes"] == {"add_note": "routes/add_note.json"}
    assert {"notes_list", "new_note"} <= set(data["places"])
    assert re.fullmatch(r"[0-9a-f]{32}", data["titleKey"]) and data["titleKey"] != "0" * 32
    assert mf.read_route(data, "add_note")["variables"] == ["text"]


def test_install_twice_changes_nothing_and_says_nothing_new(tmp_path):
    starters.install(str(tmp_path))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in _files(tmp_path)}
    text, code = starters.install(str(tmp_path))
    assert code == 0 and "nothing new to add" in text and "kept yours" not in text
    assert {p.relative_to(tmp_path): p.read_bytes() for p in _files(tmp_path)} == before


def test_install_keeps_a_route_of_the_users_and_their_places(tmp_path):
    app = "com.apple.mobilenotes"
    mf = mapfile.MapFile(tmp_path, app)
    mine = {"format": "agent-tools-script", "version": 2, "app": app, "name": "add_note", "start": "mine",
            "end": "mine", "variables": [], "steps": [{"tool": "back"}]}
    mf.update(lambda d: d["places"].__setitem__(
        "mine", {"key": {"signature": "aa", "parts": {"ids": ["Mine"], "kinds": [], "tab": None, "layer": None}},
                 "titles": {"latin": "1234"}, "last": "2026-11-01T00:00:00Z"}))
    mf.write_route("add_note", mine)
    text, code = starters.install(str(tmp_path))
    assert code == 0 and f"{app}: nothing new to add; kept yours: add_note" in text
    data, _ = mf.read()
    assert mf.read_route(data, "add_note") == mine
    assert "mine" in data["places"] and data["places"]["mine"]["titles"] == {"latin": "1234"}
    assert {"notes_list", "new_note"} <= set(data["places"])


def test_install_follows_a_place_name_the_user_already_uses_for_another_screen(tmp_path):
    app = "com.apple.mobilenotes"
    mf = mapfile.MapFile(tmp_path, app)
    mf.update(lambda d: d["places"].__setitem__(
        "notes_list", {"key": {"signature": "bb", "parts": {"ids": ["Other"], "kinds": [], "tab": None, "layer": None}},
                       "titles": {}, "last": "2026-11-01T00:00:00Z"}))
    starters.install(str(tmp_path))
    data, _ = mf.read()
    route = mf.read_route(data, "add_note")
    assert route["start"] == route["end"] != "notes_list" and route["start"] in data["places"]
    assert data["places"]["notes_list"]["key"]["signature"] == "bb"


def test_install_only_the_apps_asked_for_and_refuses_an_unknown_one(tmp_path):
    text, code = starters.install(str(tmp_path), ["com.example.nothing"])
    assert code == 2 and "No starter for com.example.nothing" in text and not list(tmp_path.iterdir())
    _, code = starters.install(str(tmp_path), ["com.apple.mobilenotes"])
    assert code == 0 and [p.name for p in tmp_path.iterdir()] == ["com.apple.mobilenotes"]


def test_install_leaves_a_map_file_that_does_not_parse_alone(tmp_path):
    app = "com.apple.mobilenotes"
    (tmp_path / app).mkdir()
    (tmp_path / app / "map.json").write_text("{not json", "utf-8")
    text, code = starters.install(str(tmp_path))
    assert code == 1 and "not installed" in text
    assert (tmp_path / app / "map.json").read_text("utf-8") == "{not json"


def test_the_command_runs_through_the_cli(tmp_path, monkeypatch, capsys):
    from tapwright import cli
    monkeypatch.setattr("sys.argv", ["tapwright", "starters", "install", "--maps", str(tmp_path)])
    assert cli.main() == 0
    assert "added add_note" in capsys.readouterr().out


def test_shipped_files_hold_no_titles_and_nothing_personal():
    """No title or text of anyone's own: a route's typed text is a ${variable}, a target is the app's own word, and no
    file has an address, a name, a UUID, a title hash or a path of this computer."""
    for path in _files(SHIPPED):
        text = path.read_text("utf-8")
        assert path.suffix == ".json", path
        data = json.loads(text)
        assert not re.search(r"@[A-Za-z0-9-]+\.[a-z]{2,}", text), path  # no e-mail address
        assert not re.search(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-", text), path  # no item's id (a UUID)
        assert not re.search(r"x-apple-reminderkit|Users[\\/]|\\\\", text), path
        assert not re.search(r"[؀-ۿ]", text), path  # none of the owner's own words in another script
        if path.name == "map.json":
            assert all(p["titles"] == {} for p in data["places"].values()), path
            assert all(r.get("names", {}) == {} for r in data["roads"]), path
        else:
            for step in data["steps"]:
                if step["tool"] == "type_text":
                    assert re.fullmatch(r"\$\{[a-z_]+\}", step["text"]), (path, step)
            assert "tapwright test" not in text.lower(), path  # the test titles are run-time values, never shipped


def test_package_data_lists_the_starters():
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text("utf-8")
    assert '"starters/*/map.json"' in pyproject and '"starters/*/routes/*.json"' in pyproject


def test_reminders_and_calendar_starters_ship_their_routes_and_places(tmp_path):
    starters.install(str(tmp_path))
    expect = {"com.apple.reminders": ({"add_reminder", "complete_reminder"}, {"reminders_lists", "new_reminder"}, "title"),
              "com.apple.mobilecal": ({"add_event", "today"}, {"calendar_day", "new_event"}, None)}
    for app, (routes, places, variable) in expect.items():
        data, found = starters.load(app)
        assert set(found) == routes and places <= set(data["places"])
        for route in found.values():
            assert route["start"] == route["end"] and route["start"] in places
        assert (variable is None) == (found.get("today") is not None and found["today"]["variables"] == [])
    assert starters.load("com.apple.reminders")[1]["add_reminder"]["variables"] == ["title"]
    assert starters.load("com.apple.mobilecal")[1]["add_event"]["variables"] == ["title"]


def test_mail_and_messages_starters_are_compose_only_drafts_routes(tmp_path):
    """compose_mail and compose_message end in a tap on Send, and are `draftsOnly`, so a drive of them stops at the draft
    (§21.3); inbox only reaches the inbox."""
    starters.install(str(tmp_path))
    mail, mail_routes = starters.load("com.apple.mobilemail")
    sms, sms_routes = starters.load("com.apple.MobileSMS")
    assert set(mail_routes) == {"compose_mail", "inbox"} and set(sms_routes) == {"compose_message"}
    assert {"inbox", "mailboxes"} <= set(mail["places"]) and {"conversations", "message_draft"} <= set(sms["places"])
    compose = mail_routes["compose_mail"]
    assert compose["variables"] == ["to", "subject", "body"] and compose["draftsOnly"] is True
    assert compose["steps"][-1] == {"tool": "tap", "target": {"id": "Mail.sendButton"}}
    assert [s["text"] for s in compose["steps"] if s["tool"] == "type_text"] == ["${to}", "${subject}", "${body}"]
    assert all(s.get("optional") for s in compose["steps"][:2])  # the button is "New Message" or "Compose"
    message = sms_routes["compose_message"]
    assert message["variables"] == ["to", "text"] and message["draftsOnly"] is True
    assert message["steps"][-1] == {"tool": "tap", "target": {"id": "sendButton"}}
    assert mail_routes["inbox"]["variables"] == [] and "draftsOnly" not in mail_routes["inbox"]
    installed = mapfile.MapFile(tmp_path, "com.apple.mobilemail")
    data, _ = installed.read()
    assert installed.read_route(data, "compose_mail")["draftsOnly"] is True
