"""`explore` through the daemon, part two (AGENT-TOOLS §25.6 to §25.10): a person first, typing and toggles, the vault, the report's
pictures, and that a second run starts from the map. The helpers are test_daemon_explore's."""
import json
from pathlib import Path

import pytest
from test_daemon import finish, run, started, until
from test_daemon_explore import explored, every_text, report_of

from tapwright.actionlog import ActionLog
from tapwright.control import Refused

VALUE = "Lisbon-Alfama-77"


def test_pause_stops_it_between_steps_with_the_report_and_where_it_was_left(tmp_path):
    async def go():
        d = explored(tmp_path)
        d.session.after_tap = lambda s: d.control.set_paused(True) if len(s.tapped) == 3 else None
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "explore", {})
        assert e.value.code == "paused"
        said = str(e.value)
        assert said.startswith("paused by the user in the live view; ask them to resume. Done: ") and " steps, " in said
        assert "Left at: place_" in said and "Report: " in said
        folder = Path(said.split("Report: ")[1]).parent
        assert json.loads((folder / "report.json").read_text("utf-8"))["stopped"] == "paused"  # the report is written first
        assert len(d.session.tapped) == 3  # not a touch after the pause
        await finish(stop, task)
    run(go)


def test_a_person_driving_stops_it_busy_after_30_seconds_of_their_input(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        d.control._people_waiting = 1  # a person is on the phone, and stays
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "explore", {})
        assert e.value.code == "busy" and "Done: " in str(e.value) and "Report: " in str(e.value)
        assert d.session.tapped == []
        await finish(stop, task)
    run(go)


def test_a_person_who_goes_quiet_lets_it_carry_on(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        looks = {"n": 0}

        def person() -> bool:
            looks["n"] += 1
            return looks["n"] <= 20  # driving for the first 20 looks, then quiet
        d.control.person_active = person
        r = await d.run_tool("a", "explore", {})
        assert "Stopped: the frontier is empty." in r["text"] and d.session.tapped
        await finish(stop, task)
    run(go)


def test_a_locked_phone_stops_it_locked(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        d.session.is_locked = True
        await until(lambda: d.state == "locked")
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "explore", {})
        assert e.value.code == "locked" and "Report: " in str(e.value)
        await finish(stop, task)
    run(go)


def test_fill_types_only_into_the_marked_field_and_a_switch_is_flipped_back(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "explore", {"fill": "test-data"})
        assert [t for t, *_ in d.session.typed] == ["Test"]  # name-test-safe only: no plain field, no secure one
        assert all(submit is False for _, _, submit, _ in d.session.typed)  # never `submit: true` (§25.6)
        assert len([k for s, k in d.session.tapped if k == "notify-switch"]) == 2  # flipped and flipped back
        assert next(e for e in d.session.screens["settings"].items if e.identifier == "notify-switch").checked is False
        await finish(stop, task)
    run(go)


def test_without_fill_nothing_is_typed(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        await d.run_tool("a", "explore", {})
        assert d.session.typed == []
        await finish(stop, task)
    run(go)


def vault(tmp_path, marked=True) -> str:
    path = tmp_path / "vault.json"
    mark = {"explore": True} if marked else {}
    path.write_text(json.dumps({"version": 2, "vault": {
        "trip": {"kind": "fixture", "destination": VALUE, **mark},
        "tester2": {"kind": "account", "username": "tester2@example.com", "password": "pw-secret-987", **mark},
        "home": {"kind": "address", "street": "12 Hidden Street", "city": "Faro", "postcode": "8000", "country": "PT"}}}), "utf-8")
    return str(path)


def test_a_fixture_is_typed_by_reference_and_its_value_is_in_no_file_answer_or_log(tmp_path):
    async def go():
        d = explored(tmp_path, actions=ActionLog(tmp_path / "log"))
        stop, task = await started(d)
        r = await d.run_tool("a", "explore", {"fill": "fixture:trip.destination", "account": "tester2"}, accounts=vault(tmp_path))
        assert [t for t, *_ in d.session.typed] == [VALUE]  # typed on the phone, by reference
        await d.maps.flush()
        shown = r["text"] + every_text(tmp_path / "explore", tmp_path / "maps", tmp_path / "log")
        for secret in (VALUE, "pw-secret-987", "tester2@example.com", "12 Hidden Street"):
            assert secret not in shown
            assert not any(secret[i:i + 12] in shown for i in range(0, max(1, len(secret) - 11)))  # no 12-character piece either
        await finish(stop, task)
    run(go)


def test_fill_and_account_name_only_entries_marked_explore(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        loose = vault(tmp_path, marked=False)
        for bad in ({"fill": "fixture:trip.destination"}, {"fill": "fixture:home.street"}, {"fill": "fixture:trip.nothing"},
                    {"fill": "fixture:ghost.x"}, {"account": "tester2"}):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "explore", bad, accounts=loose)
            assert e.value.code in ("bad_params", "not_found") and VALUE not in str(e.value), bad
        with pytest.raises(Refused) as e:  # no vault file at all
            await d.run_tool("a", "explore", {"fill": "fixture:trip.destination"})
        assert e.value.code == "bad_params"
        await finish(stop, task)
    run(go)


def test_a_private_picture_is_in_the_folder_and_report_md_names_it_without_showing_it(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        r = await d.run_tool("a", "explore", {})
        folder = report_of(r)
        body = json.loads((folder / "report.json").read_text("utf-8"))
        pictured = [f for f in body["findings"] if f["picture"]]
        assert pictured and all(f["picture"]["private"] is True for f in pictured)
        for f in pictured:
            assert (folder / f["picture"]["file"]).read_bytes()[:2] == b"\xff\xd8"  # a JPEG
        md = (folder / "report.md").read_text("utf-8")
        assert "![" not in md and "private" in md  # named, never shown unless a person asks (§27.2)
        assert md.index("## Findings") < md.index("## Skipped")  # the findings first
        await finish(stop, task)
    run(go)


def test_a_second_explore_starts_from_the_map(tmp_path):
    async def go():
        d = explored(tmp_path)
        stop, task = await started(d)
        first = await d.run_tool("a", "explore", {})
        n = len(d.session.tapped)
        second = await d.run_tool("a", "explore", {})
        assert len(d.session.tapped) - n < n  # what was tried on this build is not tried again (§25.4 item 6)
        assert report_of(first) != report_of(second)
        await finish(stop, task)
    run(go)
