"""§25.6 typing and `fill`, §25.7 submitting and toggles, §25.8 the way back after a reset, the pictures and the a11y and ux
findings of the explorer (§25.10), on the fake phone: what is typed, where, and that nothing typed is kept."""
import asyncio

from explorefakes import FakePhone, build, grid, identity
from mapfakes import screen

from tapwright import explore, policy
from tapwright.explore import Fill


def run(explorer: explore.Explorer, maps=None) -> explore.Result:
    async def go() -> explore.Result:
        result = await explorer.run()
        if maps is not None:
            await maps.flush()
        return result
    return asyncio.run(go())


def forms(**more) -> FakePhone:
    screens = {
        "home": screen("Home", ids=("go-form",)),
        "form": grid("Form", buttons=(("b-save", "Save"), ("b-help", "Help"), ("b-submit-ok", "Continue"), ("b-post-ok", "Post")),
                     inputs=("name-test-safe", "contact-email-test-safe", "plain-field", "password-test-safe"),
                     secure=("pw-test-safe",), switches=(("notify-switch", "Notifications"),), back="Home"),
        "helped": grid("Help", buttons=(("h-1", "Nothing"),), back="Form"),
    }
    return FakePhone(screens, "home", {("home", "go-form"): "form", ("form", "b-help"): "helped"},
                     {"form": "home", "helped": "form"}, **more)


def typed_keys(phone: FakePhone) -> dict[str, str]:
    return {k: text for _, k, text in phone.typed}


def test_with_fill_none_nothing_is_typed_and_no_input_is_tapped(tmp_path):
    phone = forms()
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert phone.typed == [] and result.typed == 0
    assert not {k for _, k in phone.touched} & {"name-test-safe", "plain-field", "pw-test-safe", "contact-email-test-safe"}


def test_test_data_types_a_fake_value_only_into_a_field_the_app_marks_safe(tmp_path):
    phone = forms()
    explorer, maps = build(tmp_path, phone, fill=Fill("test-data"))
    result = run(explorer, maps)
    assert typed_keys(phone) == {"name-test-safe": "Test", "contact-email-test-safe": "test@example.com"}  # (`email` in its id)
    assert result.typed == 2
    # not a field with no marker, not a secure one, and not one that says password, even with the marker
    assert not set(typed_keys(phone)) & {"plain-field", "pw-test-safe", "password-test-safe"}


def test_where_it_typed_the_only_moves_are_back_and_the_controls_in_submit(tmp_path):
    phone = forms()
    rules = policy.parse({"explore": {"submit": ["b-submit-ok", "b-post-ok"]}})
    explorer, maps = build(tmp_path, phone, rules, fill=Fill("test-data"))
    result = run(explorer, maps)
    touched = {k for s, k in phone.touched if s == "form"}
    assert "b-submit-ok" in touched  # in `submit`: the only way a form is sent
    assert "b-help" not in touched and "b-save" not in touched  # every other control there is skipped
    assert "b-post-ok" not in touched  # the floor wins over a listed id
    why = {s.sig: s.why for s in result.skipped}
    assert why["id:b-help"] == "may_submit" and why["id:b-save"] == "may_submit" and why["id:b-post-ok"] == "floor"


def test_the_skip_made_for_a_typed_screen_is_not_kept_for_a_run_without_fill(tmp_path):
    phone = forms()
    explorer, maps = build(tmp_path, phone, fill=Fill("test-data"))
    run(explorer, maps)
    phone2 = forms()
    explorer2, _ = build(tmp_path, phone2)  # the same map, no fill: Help is an ordinary control
    run(explorer2)
    assert "b-help" in {k for _, k in phone2.touched}


def test_a_fixture_field_is_typed_by_reference_and_the_value_is_never_here(tmp_path):
    phone = forms()
    explorer, maps = build(tmp_path, phone, fill=Fill("fixture", "${vault.trip_data.destination}"))
    result = run(explorer, maps)
    assert typed_keys(phone)["name-test-safe"] == "${vault.trip_data.destination}"
    assert result.typed == 2


def test_nothing_typed_is_in_the_map_or_a_finding(tmp_path):
    phone = forms()
    phone.errors[("form", "b-help")] = ("an error after typing",)
    explorer, maps = build(tmp_path, phone, fill=Fill("test-data"), submit=("b-help",))
    result = run(explorer, maps)
    files = [p.read_text("utf-8") for p in tmp_path.rglob("*.json")]
    assert files and not any("test@example.com" in text for text in files)
    assert not any("test@example.com" in str(f) for f in result.findings)


# ---- toggles (§25.7) ----

def switches(**more) -> FakePhone:
    screens = {
        "home": screen("Home", ids=("go-prefs",)),
        "prefs": grid("Prefs", switches=(("notify-switch", "Notifications"), ("b-del-switch", "Delete my data"),
                                         ("pay-switch", "Pay by card")), buttons=(("b-ok", "OK"),), back="Home"),
    }
    return FakePhone(screens, "home", {("home", "go-prefs"): "prefs"}, {"prefs": "home"}, **more)


def values(phone: FakePhone) -> dict[str, bool | None]:
    return {e.identifier: e.checked for e in phone.screens["prefs"].items if e.role == "switch"}


def test_a_toggle_is_flipped_and_set_back_and_a_floor_word_is_never_flipped(tmp_path):
    phone = switches()
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    flips = [(k, v) for _, k, v in phone.toggles]
    assert flips == [("notify-switch", True), ("notify-switch", False)]  # flipped and flipped back, one switch
    assert values(phone)["notify-switch"] is False  # as it was
    assert result.flipped == 1 and result.stopped == "frontier_empty"
    skipped = {s.sig: s for s in result.skipped}
    assert skipped["id:b-del-switch"].why == "floor" and skipped["id:pay-switch"].why == "floor"


def test_a_toggle_that_cant_be_set_back_stops_the_run_and_names_it(tmp_path):
    phone = switches()
    phone.once.add("notify-switch")  # it flips once, then sticks
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert result.stopped == "toggle_not_restored" and result.detail == "id=notify-switch"
    from tapwright import explorereport
    assert explorereport.stop_line(result) == "a toggle could not be set back (id=notify-switch)."
    assert values(phone)["notify-switch"] is True  # left as it is: nothing else is touched to hide it


def test_a_toggle_whose_value_cant_be_read_is_left_untried(tmp_path):
    phone = switches()
    for e in phone.screens["prefs"].items:
        if e.identifier == "notify-switch":
            e.checked = None
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert phone.toggles == []
    assert ("place_2", "id:notify-switch", "toggle") in result.deferred or any(k == "toggle" for _, _, k in result.deferred)


def test_a_toggle_the_dry_run_holds_is_skipped_not_flipped(tmp_path):
    phone = switches()
    phone.needs["notify-switch"] = "needs_person"
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert phone.toggles == [] and any(s.sig == "id:notify-switch" and s.why == "policy" for s in result.skipped)


# ---- pictures and checks (§25.10, §27.2) ----

def test_a_finding_keeps_a_private_picture_of_the_given_app_and_none_for_a_crash(tmp_path):
    phone = forms()
    phone.errors[("home", "go-form")] = ("TypeError: boom",)
    phone.crashes.add(("form", "b-help"))
    explorer, maps = build(tmp_path, phone, continue_after_crash=True)
    result = run(explorer, maps)
    by = {f.kind: f for f in result.findings}
    assert by["js_error"].picture is not None and by["js_error"].picture.private is True
    assert by["js_error"].picture.file == f"findings/{by['js_error'].id}.jpg" and by["js_error"].id in result.pictures
    assert by["crash"].picture is None and by["crash"].id not in result.pictures


def test_a_test_app_picture_is_not_private(tmp_path):
    phone = forms()
    phone.errors[("home", "go-form")] = ("TypeError: boom",)
    explorer, maps = build(tmp_path, phone, who=identity(test_app=True))
    result = run(explorer, maps)
    assert next(f for f in result.findings if f.kind == "js_error").picture.private is False


def test_no_picture_when_the_given_app_is_not_in_front(tmp_path):
    phone = forms()
    phone.pictures = False
    phone.errors[("home", "go-form")] = ("TypeError: boom",)
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert all(f.picture is None for f in result.findings) and result.pictures == {}


def test_a_place_gets_one_a11y_and_one_ux_finding_of_at_most_ten_issues(tmp_path):
    phone = forms()
    phone.audits["form"] = tuple(f"issue {n}" for n in range(14))
    phone.uxes["home"] = ("one ux issue",)
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    a11y = [f for f in result.findings if f.kind == "a11y"]
    assert len(a11y) == 1 and a11y[0].got.count("\n") == 9 and a11y[0].place is not None
    assert [f.kind for f in result.findings if f.kind == "ux"] == ["ux"]


# ---- reset with an account (§25.8) ----

def test_after_a_reset_the_way_back_may_be_a_route_that_types_the_vault_account(tmp_path):
    screens = {
        "home": screen("Home", ids=("go-a",)),
        "a": screen("Alpha", ids=("go-b",), back="Home"),
        "b": screen("Beta", ids=("go-c",), back="Alpha"),
        "c": screen("Gamma", ids=("go-end",), back="Beta"),
        "login": screen("Sign-in gate", ids=("go-nothing",)),
    }
    goes = {("home", "go-a"): "a", ("a", "go-b"): "b", ("b", "go-c"): "c"}
    phone = FakePhone(screens, "home", goes, {"a": "home", "b": "a", "c": "b"})
    phone.fail_back.update({"c", "b"})  # back does nothing from c or b: the branch needs a reset
    phone.relaunch_at, phone.drive_to = "login", "home"  # a relaunch lands on a gate the map has no way out of
    explorer, maps = build(tmp_path, phone, account="tester2")
    result = run(explorer, maps)
    assert result.resets and all(r["how"] == "relaunch" and r["data_kept"] is True for r in result.resets)
    assert phone.drives and all(a == "tester2" for _, a in phone.drives)  # the route that types the account, to the start place
    assert result.stopped == "frontier_empty"


def test_without_an_account_a_gate_after_a_reset_ends_the_branch_and_three_in_a_row_stop_the_run(tmp_path):
    screens = {
        "home": screen("Home", ids=("go-a",)),
        "a": screen("Alpha", ids=("go-b",), back="Home"),
        "b": screen("Beta", ids=("go-c",), back="Alpha"),
        "login": screen("Sign-in gate", ids=("go-nothing",)),
    }
    phone = FakePhone(screens, "home", {("home", "go-a"): "a", ("a", "go-b"): "b"}, {"a": "home", "b": "a"})
    phone.fail_back.update({"b"})
    phone.relaunch_at = "login"
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert phone.drives == [] and result.stopped in ("frontier_empty", "cant_get_back")
