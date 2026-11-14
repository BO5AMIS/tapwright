"""§25.13 Safety and Scope, on a red-team set of screens: the explorer touches none of the dangerous buttons, types in none of
the fields, never allows a prompt, never leaves the app, and checks the floor afresh at every touch."""
import asyncio
from dataclasses import replace

from explorefakes import FakePhone, OTHER, build, grid, identity
from mapfakes import screen

from tapwright import explore, explored, policy

DANGER = (("b-delete", "Delete"), ("b-signout", "Sign out"), ("b-pay", "Pay"), ("b-send", "Send"), ("b-block", "Block"),
          ("delete-row", ""), ("b-buy", "Buy now"), ("b-report", "Report"), ("b-reply", "Reply"), ("b-erase", "Erase"),
          ("b-post-save", "Post"), ("b-delacct", "Delete account"), ("b-card", "Add card"), ("b-sub", "Subscribe"))
SAFE = (("b-open", "Open details"), ("b-help", "Help"))


def run(explorer: explore.Explorer, maps=None) -> explore.Result:
    async def go() -> explore.Result:
        result = await explorer.run()
        if maps is not None:
            await maps.flush()
        return result
    return asyncio.run(go())


def red_team(**more) -> FakePhone:
    screens = {
        "home": screen("Home", ids=("go-actions", "go-form", "go-prompt")),
        "actions": grid("Actions", buttons=(*DANGER, *SAFE), back="Home"),
        "form": grid("Form", buttons=(("b-save", "Save changes"), ("b-continue", "Continue"), ("b-more", "More options")),
                     inputs=("name-field",), secure=("pw-field",), switches=(("notify-switch", "Notifications"),), back="Home"),
        "prompt": grid("Prompt", buttons=(("ask", "Ask"),), back="Home"),
        "asking": grid("Prompt", buttons=(("ask", "Ask"),), alert=("“Wayfare” Would Like to Send You Notifications",
                                                                    ("Allow", "Don’t Allow")), back="Home"),
        "share": grid("Prompt", buttons=(("ask", "Ask"),), popover=True),
    }
    goes = {("home", "go-actions"): "actions", ("home", "go-form"): "form", ("home", "go-prompt"): "prompt"}
    back_to = {"actions": "home", "form": "home", "prompt": "home", "asking": "home", "share": "prompt"}
    return FakePhone(screens, "home", goes, back_to, **more)


def keys(phone: FakePhone, where: str) -> set[str]:
    return {k for s, k in phone.touched if s == where}


def test_the_red_team_screens_none_of_the_dangerous_buttons_is_touched(tmp_path):
    phone = red_team()
    explorer, maps = build(tmp_path, phone)
    result = run(explorer, maps)
    assert result.stopped == "frontier_empty"
    assert keys(phone, "actions") == {"b-open", "b-help"}
    dangerous = {k for k, _ in DANGER}
    assert not dangerous & {k for _, k in phone.touched}
    skipped = {s.sig: s for s in result.skipped}
    assert skipped["id:delete-row"].why == "floor" and skipped["id:delete-row"].word == "Delete"  # the icon button by its id
    assert skipped["id:b-post-save"].why == "floor" and skipped["id:b-post-save"].word == "Post"
    assert skipped["id:b-signout"].why == "floor" and skipped["id:b-card"].why == "floor"
    assert all(s.why in explored.WHYS for s in result.skipped)
    data = maps.data(str(tmp_path), "com.example.app")
    here = next(r["to"] for r in data["roads"] if r["move"]["target"] == {"id": "go-actions"})
    saved = explored.action(data, here, "id:b-delete")
    assert saved is not None and saved["result"] == "skipped" and saved["why"] == "floor"


def test_a_control_in_the_submit_list_that_also_says_post_is_never_touched(tmp_path):
    rules = policy.parse({"explore": {"submit": ["b-post-save"]}})
    phone = red_team()
    run(build(tmp_path, phone, rules)[0])
    assert "b-post-save" not in {k for _, k in phone.touched}


def test_no_field_is_typed_in_or_tapped_a_switch_is_left_alone_and_a_may_submit_control_is_skipped(tmp_path):
    phone = red_team()
    result = run(build(tmp_path, phone)[0])
    touched = {k for _, k in phone.touched}
    assert not touched & {"name-field", "pw-field"}  # a text input is only read, a secure one never touched
    assert [(k, v) for _, k, v in phone.toggles if k == "notify-switch"] == [("notify-switch", True), ("notify-switch", False)]
    assert not result.deferred  # a switch is flipped and set back (§25.7)
    form = {s.sig: s for s in result.skipped}
    assert form["id:b-save"].why == "may_submit" and form["id:b-continue"].why == "may_submit"
    assert "b-more" in touched  # a control with no submit word is an ordinary tap


def test_a_permission_prompt_is_answered_dont_allow_and_never_allowed(tmp_path):
    phone = red_team()
    phone.goes[("prompt", "ask")] = "asking"
    phone.alert_to["asking"] = "prompt"
    result = run(build(tmp_path, phone)[0])
    assert phone.answered == ["Don’t Allow"] and result.prompts == 1
    assert not any(f.kind == "dead_end" for f in result.findings)  # a prompt is not a finding


def test_an_alert_it_may_not_answer_is_left_and_the_app_is_relaunched(tmp_path):
    phone = red_team()
    phone.screens["asking"] = grid("Prompt", buttons=(("ask", "Ask"),), alert=("Delete everything?", ("Delete",)))
    phone.goes[("prompt", "ask")] = "asking"
    result = run(build(tmp_path, phone)[0])
    assert phone.answered == [] and phone.opens >= 1
    assert any("alert it may not answer" in n for n in result.notes)


def test_a_share_sheet_is_not_followed_it_is_a_dead_end_closed_by_back(tmp_path):
    phone = red_team()
    phone.goes[("prompt", "ask")] = "share"
    result = run(build(tmp_path, phone)[0])
    assert ("share", "Copy") not in phone.touched  # nothing on the sheet is touched
    assert any(f.kind == "dead_end" and "left the app" in f.got for f in result.findings)
    assert phone.backs >= 1


def test_a_move_that_leaves_for_another_app_touches_nothing_there(tmp_path):
    phone = red_team()
    phone.screens["safari"] = grid("Safari", buttons=(("web-delete", "Delete"), ("web-link", "Link")), bundle=OTHER)
    phone.goes[("actions", "b-open")] = "safari"
    result = run(build(tmp_path, phone)[0])
    assert phone.foreign_touched == [] and not any(s == "safari" for s, _ in phone.touched)
    assert [f.kind for f in result.findings].count("dead_end") == 1
    assert phone.opens >= 1 and result.stopped == "frontier_empty"


def test_a_screen_whose_app_cant_be_told_is_not_touched(tmp_path):
    phone = red_team()
    unknown = grid("Mystery", buttons=(("m-open", "Open"),))
    unknown.app = None  # the read doesn't say which app is in front
    phone.screens["mystery"] = unknown
    phone.goes[("actions", "b-help")] = "mystery"
    run(build(tmp_path, phone)[0])
    assert not any(s == "mystery" for s, _ in phone.touched)


def test_when_the_dry_run_says_a_person_is_needed_the_touch_is_not_made_and_nobody_is_asked(tmp_path):
    phone = red_team()
    phone.needs["b-help"] = "needs_person"
    phone.needs["b-open"] = "blocked"
    result = run(build(tmp_path, phone)[0])
    assert not keys(phone, "actions")
    assert {"id:b-help", "id:b-open"} <= {s.sig for s in result.skipped if s.why == "policy"}


def test_the_floor_is_checked_afresh_at_every_touch_a_saved_skip_or_a_saved_try_allows_nothing(tmp_path):
    # run 1: a policy that doesn't hold "Help"; it is tried. Run 2 on a new build with a policy that does: not touched again.
    first = red_team()
    explorer, maps = build(tmp_path, first)
    run(explorer, maps)
    assert "b-help" in keys(first, "actions")
    rules = policy.parse({"confirm": ["Help"]})
    second = red_team()
    run(build(tmp_path, second, rules, who=replace(identity(), build="1.6.0 (230)"), port=explorer.map)[0])
    assert "b-help" not in keys(second, "actions")
    # and the other way: a skip saved under a skip list stays a skip when the list is gone (it holds for every build)
    third = red_team()
    other, maps3 = build(tmp_path / "x", third, policy.parse({"explore": {"skip": ["Help"]}}))
    run(other, maps3)
    fourth = red_team()
    run(build(tmp_path / "x", fourth, who=replace(identity(), build="1.7.0 (240)"), port=other.map)[0])
    assert "b-help" not in keys(fourth, "actions")
    assert "b-open" in keys(fourth, "actions")  # (an unrelated action is tried again on a new build)


def test_a_policy_with_no_confirm_words_or_no_policy_does_not_lower_the_floor(tmp_path):
    for n, rules in enumerate((policy.NONE, policy.parse({"confirm": [], "block": []}))):
        phone = red_team()
        run(build(tmp_path / str(n), phone, rules)[0])
        assert not {k for k, _ in DANGER} & {k for _, k in phone.touched}


def test_the_users_skip_list_adds_to_the_floor_and_the_policy_files_words_are_skipped_as_policy(tmp_path):
    rules = policy.parse({"confirm": ["Help"], "explore": {"skip": ["Open"]}})
    phone = red_team()
    result = run(build(tmp_path, phone, rules)[0])
    assert not keys(phone, "actions")
    why = {s.sig: s.why for s in result.skipped}
    assert why["id:b-help"] == "policy" and why["id:b-open"] == "skip_list"


def test_a_row_with_a_delete_button_inside_is_never_touched(tmp_path):
    phone = red_team()
    phone.screens["actions"] = grid("Actions", cells=(("Trip", ("Delete",)), ("Other trip", ())), buttons=(("b-open", "Open"),),
                                    back="Home")
    result = run(build(tmp_path, phone)[0])
    assert "Trip" not in keys(phone, "actions") and "Other trip" in keys(phone, "actions")
    assert any(s.why == "floor" and s.word == "Delete" for s in result.skipped)


def test_a_screen_titled_privacy_is_saved_and_not_expanded(tmp_path):
    phone = red_team()
    phone.screens["actions"] = grid("Privacy", buttons=SAFE, back="Home")
    result = run(build(tmp_path, phone)[0])
    assert not keys(phone, "actions")
    assert any(s.why == "floor" and s.word == "Privacy" for s in result.skipped)
    assert any("privacy or security" in n for n in result.notes)


def test_settings_as_the_app_is_all_system_and_nothing_is_touched(tmp_path):
    settings = "com.apple.Preferences"
    screens = {"top": grid("Settings", buttons=(("wifi", "Wi-Fi"), ("general", "General")), bundle=settings)}
    phone = FakePhone(screens, "top")
    from tapwright import findings
    who = explore.Identity(settings, "26.5", "26.5 (1)", findings.Device("iPhone 14", "26.5"), "1.8.0")
    explorer = explore.Explorer(phone, explore.MapsPort(__import__("tapwright").mapkeeper.Maps(str(tmp_path)), str(tmp_path), settings),
                                policy.DEFAULT, who, clock=phone.clock)
    result = run(explorer)
    assert phone.touched == [] and {s.why for s in result.skipped} == {"system"}


def test_every_move_is_a_tap_a_back_a_scroll_or_an_open_never_a_long_press_or_a_swipe(tmp_path):
    phone = red_team()
    assert not any(hasattr(phone, name) for name in ("long_press", "swipe"))
    run(build(tmp_path, phone)[0])  # the fake has no such methods: a call to one would have raised
