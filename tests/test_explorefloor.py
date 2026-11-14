"""The skip floor (§25.5): every source of it, that a user's skip list only adds, and the system screens and alerts."""
import pytest
from explorefakes import grid

from tapwright import explorefloor, grants, policy, ui
from tapwright.explorefloor import Floor

APP = "com.example.app"


def button(label: str, ident: str = "", role: str = "button", **more) -> ui.Element:
    return ui.Element("e1", role, label, ident, rect=(10, 10, 100, 40), actions=("tap",), type="Button", **more)


def floor(rules: policy.Policy = policy.DEFAULT, **more) -> Floor:
    return Floor(rules, APP, **more)


def why(rules: policy.Policy, label: str, ident: str = "", **more) -> tuple[str, str | None] | None:
    hit = floor(rules).element(button(label, ident, **more))
    return None if hit is None else (hit.why, hit.word)


@pytest.mark.parametrize("label,word", [("Delete trip", "Delete"), ("Sign out", "Sign out"), ("Erase all", "Erase"),
                                        ("Log out", "Log out"), ("Remove", "Remove")])
def test_the_built_in_confirm_words_are_the_floor(label, word):
    assert why(policy.DEFAULT, label) == ("floor", word)


@pytest.mark.parametrize("label", ["Send", "Post", "Publish", "Reply", "Comment", "Report", "Block", "Block user",
                                   "Report a problem"])
def test_the_reach_others_words_and_report_and_block_are_the_floor(label):
    assert why(policy.NONE, label) is not None and why(policy.NONE, label)[0] == "floor"


@pytest.mark.parametrize("label", ["Buy", "Purchase", "Pay", "Subscribe", "Pay now", "Delete account", "Close account",
                                   "Add card", "Credit card", "Checkout", "Donate"])
def test_the_never_grantable_list_is_the_floor(label):
    assert why(policy.NONE, label) is not None and why(policy.NONE, label)[0] == "floor"


def test_the_policy_files_confirm_and_block_words_are_the_floor_global_and_the_apps_together():
    rules = policy.parse({"confirm": ["Archive"], "block": ["Wipe"],
                          "apps": {APP: {"confirm": ["Escalate"], "block": ["Nuke"]}, "other.app": {"block": ["Zap"]}}})
    for label in ("Archive", "Wipe", "Escalate", "Nuke"):  # the app's own lists don't replace the global ones for the explorer
        assert why(rules, label) == ("policy", label)
    assert why(rules, "Zap") is None  # another app's words aren't this app's


def test_an_empty_confirm_list_or_no_policy_does_not_lower_the_floor():
    for rules in (policy.parse({"confirm": [], "block": []}), policy.NONE, policy.parse({"apps": {APP: {"confirm": []}}})):
        assert why(rules, "Delete trip") == ("floor", "Delete")
        assert why(rules, "Sign out") == ("floor", "Sign out")


def test_a_users_skip_list_only_adds_to_the_floor():
    rules = policy.parse({"explore": {"skip": ["Archive"]}})
    assert why(rules, "Archive") == ("skip_list", "Archive")
    assert why(rules, "Delete trip") == ("floor", "Delete")  # still there
    assert why(policy.parse({"explore": {"skip": []}}), "Delete trip") == ("floor", "Delete")  # an empty list removes nothing
    assert why(policy.DEFAULT, "Archive") is None
    assert floor(skip=("Mute",)).element(button("Mute chat")) == explorefloor.Skip("skip_list", "Mute")


def test_matching_is_whole_words_ignoring_case_and_on_the_label_or_text_only():
    assert why(policy.DEFAULT, "DELETE trip") == ("floor", "Delete")
    assert why(policy.DEFAULT, "Deleted items") is None  # a whole word
    assert why(policy.DEFAULT, "Open", ident="delete-button") is None  # an id with a label is never matched (§11.1)
    assert why(policy.DEFAULT, "Don’t post", ) == ("floor", "Post")


def test_an_icon_button_with_no_label_is_matched_on_its_id_split_into_words():
    assert why(policy.DEFAULT, "", "delete-row") == ("floor", "Delete")
    assert why(policy.DEFAULT, "", "deleteRow") == ("floor", "Delete")
    assert why(policy.DEFAULT, "", "sign_out_btn") == ("floor", "Sign out")
    assert why(policy.DEFAULT, "", "open-details") is None


def test_an_input_is_matched_on_its_label_never_on_what_was_typed():
    typed = ui.Element("e1", "input", "Note", "note", value="Delete everything", rect=(0, 0, 10, 10), actions=("tap", "type"))
    assert floor().element(typed) is None


def test_a_touch_is_checked_on_everything_it_could_land_on():
    row = ui.Element("e1", "cell", "Row", "", rect=(0, 0, 200, 50), actions=("tap",))
    inner = ui.Element("e2", "button", "Delete", "", rect=(150, 5, 40, 40), actions=("tap",))
    other = ui.Element("e3", "button", "Send", "", rect=(0, 0, 200, 50), actions=("tap",))  # drawn over the row, in front
    assert floor().touch(row, [row, inner]).word == "Delete"  # a button inside the row
    assert floor().touch(row, [row, other]).word == "Send"  # what is in front of its centre
    clean = ui.Element("e4", "cell", "Row", "", rect=(0, 100, 200, 50), actions=("tap",))
    assert floor().touch(clean, [row, inner, clean]) is None


def test_a_control_with_a_submit_word_on_a_screen_with_an_input_may_submit():
    field = ui.Element("e0", "input", "Name", "name", rect=(0, 0, 100, 40), actions=("tap", "type"))
    for label in ("Save", "Continue", "Sign in", "Log in", "Sign up", "Register", "Create trip", "Confirm", "Apply", "Update",
                  "Share"):
        skip = floor().may_submit(button(label), [field])
        assert skip is not None and skip.why == "may_submit", label
    assert floor().may_submit(button("Save"), []) is None  # no input on the screen
    assert floor().may_submit(button("Open"), [field]) is None


def test_settings_wallet_and_passwords_are_system_screens_whatever_is_on_them():
    tree = grid("Wi-Fi", buttons=(("a", "Open"),))
    for bundle in grants.NEVER_APPS:
        assert floor().screen(tree, bundle) == explorefloor.Skip("system")
    assert floor().screen(tree, APP) is None


def test_a_share_sheet_or_a_purchase_sheet_is_a_system_screen():
    assert floor().screen(grid("Trip", buttons=(("a", "Open"),), popover=True)) == explorefloor.Skip("system")


def test_an_account_privacy_or_security_title_holds_the_whole_screen():
    for title in ("Account", "Privacy", "Security", "Passwords", "Payment methods", "Billing"):
        skip = floor().screen(grid(title, buttons=(("a", "Open"),)))
        assert skip is not None and skip.why == "floor", title
    skip = floor().screen(grid("Profile", buttons=(("a", "Open"),), headers=("Privacy",)))
    assert skip is not None and skip.word == "Privacy"
    assert floor().screen(grid("Profile", buttons=(("a", "Open"),))) is None


def alert(title: str, *buttons: str, message: str = "") -> ui.Alert:
    return ui.Alert(title, tuple(buttons), (0, 0, 10, 10), message)


def test_a_permission_prompt_is_answered_dont_allow_never_allow():
    f = floor()
    assert explorefloor.safe_button(alert("“Wayfare” Would Like to Send You Notifications", "Don’t Allow", "Allow"), f) == "Don’t Allow"
    assert explorefloor.safe_button(alert("Allow tracking?", "Allow", "Ask App Not to Track"), f) == "Ask App Not to Track"
    assert explorefloor.safe_button(alert("Location", "Allow While Using App", "Allow Once", "Don't Allow"), f) == "Don't Allow"
    assert explorefloor.safe_button(alert("Rate us", "Not Now", "Rate"), f) == "Not Now"


def test_an_alert_with_a_floor_word_is_answered_by_cancel_and_one_it_may_not_answer_is_not_answered():
    f = floor()
    assert explorefloor.safe_button(alert("Delete trip?", "Delete", "Cancel"), f) == "Cancel"
    assert explorefloor.safe_button(alert("Delete trip?", "Delete"), f) is None
    assert explorefloor.safe_button(alert("Heads up", "OK"), f) == "OK"  # the only way to go on, nothing held by the floor
    assert explorefloor.safe_button(alert("Delete everything?", "OK"), f) is None  # an OK on a destructive message
    assert explorefloor.safe_button(alert("Which?", "Allow", "Always"), f) is None  # never an Allow
