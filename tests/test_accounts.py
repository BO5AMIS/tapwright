"""Test accounts (AGENT-TOOLS §18.6): the file, the git rule, what an account may be typed into, and what is scrubbed."""
import json
import shutil
import subprocess

import pytest

from tapwright import accounts
from tapwright.control import Refused

FILE = {"version": 1, "accounts": {
    "tester2": {"username": "tester2@example.com", "password": "hunter2-PW", "apps": ["com.example.wayfare"]},
    "tester1": {"username": "tester1@example.com", "password": "correct-horse"},
    "phone": {"passcode": "482913"}}}


def write(tmp_path, data=FILE, name="accounts.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), "utf-8")
    return path


def test_an_accounts_file_is_read(tmp_path):
    loaded = accounts.load(str(write(tmp_path)))
    assert loaded.problem is None
    assert loaded.names == ("tester2", "tester1")  # (the phone's passcode isn't an account anyone drives with)
    two = loaded.accounts["tester2"]
    assert (two.username, two.password, two.apps) == ("tester2@example.com", "hunter2-PW", ("com.example.wayfare",))
    assert loaded.accounts["tester1"].apps is None


def test_the_passcode_is_kept_and_shown_nowhere(tmp_path):
    loaded = accounts.load(str(write(tmp_path)))
    assert loaded.passcode == "482913"
    assert "482913" not in repr(loaded) and "482913" not in str(loaded.names)
    assert "hunter2-PW" not in repr(loaded)  # a value never shows in a repr, a log line or a traceback


@pytest.mark.parametrize("data, why", [
    ({"version": 2, "accounts": {}}, "version"),
    ({"version": 1}, "accounts"),
    ({"version": 1, "accounts": []}, "accounts"),
    ({"version": 1, "accounts": {"a b": {"username": "x", "password": "y"}}}, "name"),
    ({"version": 1, "accounts": {"a": {"username": 3}}}, "username"),
    ({"version": 1, "accounts": {"a": {"color": "red"}}}, "color"),
    ({"version": 1, "accounts": {"a": {"username": "xyz", "apps": "com.a"}}}, "apps"),
    ([], "object"),
])
def test_a_file_that_isnt_right_is_a_problem_not_a_crash(tmp_path, data, why):
    loaded = accounts.load(str(write(tmp_path, data)))
    assert loaded.problem and why in loaded.problem and loaded.names == ()


def test_a_missing_or_unreadable_file_is_a_problem(tmp_path):
    assert "can't be read" in accounts.load(str(tmp_path / "nope.json")).problem
    (tmp_path / "bad.json").write_text("{not json", "utf-8")
    assert "isn't JSON" in accounts.load(str(tmp_path / "bad.json")).problem


def test_an_account_is_found_by_name_and_limited_to_its_apps(tmp_path):
    loaded = accounts.load(str(write(tmp_path)))
    assert loaded.account("tester2", "com.example.wayfare").username == "tester2@example.com"
    assert loaded.account("tester1", "com.anything").name == "tester1"
    with pytest.raises(Refused) as e:
        loaded.account("tester2", "com.other")
    assert e.value.code == "not_allowed" and "com.example.wayfare" in str(e.value)
    with pytest.raises(Refused) as e:
        loaded.account("nobody", "com.example.wayfare")
    assert e.value.code == "not_found" and "tester2, tester1" in str(e.value)
    with pytest.raises(Refused) as e:
        loaded.account("phone", "com.example.wayfare")  # the phone entry holds a passcode only: not an account to drive with
    assert e.value.code == "not_found"


def test_a_problem_refuses_every_use_with_its_reason(tmp_path):
    loaded = accounts.load(str(write(tmp_path, {"version": 1})))
    with pytest.raises(Refused) as e:
        loaded.account("a", "com.x")
    assert e.value.code == "bad_params" and "accounts" in str(e.value)


def test_no_file_given_says_how_to_give_one():
    with pytest.raises(Refused) as e:
        accounts.NONE.account("tester2", "com.x")
    assert e.value.code == "bad_params" and "--accounts" in str(e.value)
    assert accounts.NONE.names == ()


def test_a_value_is_typed_only_where_it_may_be():
    two = accounts.Account("tester2", "tester2@example.com", "hunter2-PW", None)
    assert accounts.typed("${account.username}", two, False, "e1") == "tester2@example.com"
    assert accounts.typed("${account.password}", two, True, "e2") == "hunter2-PW"
    with pytest.raises(Refused) as e:
        accounts.typed("${account.password}", two, False, "e14")
    assert e.value.code == "bad_params"
    assert str(e.value) == "an account's password goes only into a password field; e14 is a text field."
    with pytest.raises(Refused) as e:
        accounts.typed("${account.username}", two, True, "e3")
    assert "username goes only into a text field that isn't secure; e3 is a password field" in str(e.value)


def test_a_value_is_the_whole_text_never_part_of_one():
    two = accounts.Account("tester2", "tester2@example.com", "hunter2-PW", None)
    for text in ("hi ${account.password}", "${account.password}!", "${account.username}${account.password}"):
        with pytest.raises(Refused) as e:
            accounts.typed(text, two, True, "e1")
        assert e.value.code == "bad_params" and "whole text" in str(e.value)
    assert accounts.typed("plain", two, False, "e1") == "plain"
    assert accounts.mentions("a ${account.password} b") and not accounts.mentions("plain")


def test_no_account_active_means_no_value():
    with pytest.raises(Refused) as e:
        accounts.typed("${account.password}", None, True, "e1")
    assert e.value.code == "bad_params" and "drive" in str(e.value)


def test_an_account_without_a_password_cant_type_one():
    one = accounts.Account("u", "u@example.com", None, None)
    with pytest.raises(Refused) as e:
        accounts.typed("${account.password}", one, True, "e1")
    assert "no password" in str(e.value)


def test_what_is_scrubbed_is_every_value_of_every_account(tmp_path):
    loaded = accounts.load(str(write(tmp_path)))
    text = 'e5 input "Email" value="tester2@example.com"; pw hunter2-PW; other correct-horse; tester1@example.com'
    out = loaded.scrub(text)
    for secret in ("tester2@example.com", "hunter2-PW", "correct-horse", "tester1@example.com", "482913"):
        assert secret not in out
    assert "<tester2's username>" in out and "<tester2's password>" in out and "<tester1's password>" in out
    assert loaded.scrub("nothing to hide") == "nothing to hide"
    assert accounts.NONE.scrub("anything") == "anything"


def test_short_values_are_not_scrubbed_out_of_ordinary_words(tmp_path):
    loaded = accounts.load(str(write(tmp_path, {"version": 1, "accounts": {"t": {"username": "ab", "password": "xy"}}})))
    assert loaded.scrub("a table and an xylophone") == "a table and an xylophone"


def test_a_credential_too_short_to_be_scrubbed_is_not_accepted(tmp_path):
    # (CodeRabbit PR #9) "12" would stay in answers and maps: the file is refused rather than kept half-protected
    for key in ("username", "password"):
        loaded = accounts.load(str(write(tmp_path, {"version": 1, "accounts": {"t": {key: "12"}}})))
        assert loaded.problem is not None and key in loaded.problem and loaded.names == ()
    ok = accounts.load(str(write(tmp_path, {"version": 1, "accounts": {"t": {"password": "123"}}})))
    assert ok.problem is None and ok.names == ("t",)


def test_names_that_hold_a_value_are_dropped_before_the_map_keeps_them(tmp_path):
    loaded = accounts.load(str(write(tmp_path)))
    assert loaded.clean({"id": "email", "label": "Email", "text": "tester2@example.com"}) == {"id": "email", "label": "Email"}
    assert loaded.holds("tester2@example.com") and loaded.holds("my hunter2-PW here") and not loaded.holds("Email")


GIT = shutil.which("git")


@pytest.mark.skipif(GIT is None, reason="git isn't installed here")
def test_a_file_inside_a_git_repo_that_doesnt_ignore_it_loads_no_accounts(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = write(tmp_path)
    loaded = accounts.load(str(path))
    assert loaded.names == () and loaded.problem is not None
    assert loaded.problem.startswith("accounts file is inside the git repo at ")
    assert loaded.problem.endswith(" and not ignored; move it or add it to .gitignore.")
    with pytest.raises(Refused) as e:
        loaded.account("tester1", "com.x")
    assert e.value.code == "bad_params" and str(e.value) == loaded.problem


@pytest.mark.skipif(GIT is None, reason="git isn't installed here")
def test_a_file_the_repo_ignores_loads(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("accounts.json\n", "utf-8")
    loaded = accounts.load(str(write(tmp_path)))
    assert loaded.problem is None and loaded.names == ("tester2", "tester1")


@pytest.mark.skipif(GIT is None, reason="git isn't installed here")
def test_a_tracked_file_is_not_ignored_even_when_a_pattern_matches(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = write(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "accounts.json"], check=True)
    (tmp_path / ".gitignore").write_text("accounts.json\n", "utf-8")
    assert accounts.load(str(path)).problem is not None


def test_a_file_outside_any_repo_loads(tmp_path):
    assert accounts.load(str(write(tmp_path)), git=lambda args, cwd: None).problem is None


def test_the_store_reads_a_file_again_when_it_changes(tmp_path):
    path = write(tmp_path)
    store = accounts.Store()
    assert store.get(str(path)).names == ("tester2", "tester1")
    assert store.get(str(path)) is store.get(str(path))
    write(tmp_path, {"version": 1, "accounts": {"only": {"username": "o@example.com", "password": "pw-pw-pw"}}})
    assert store.get(str(path)).names == ("only",)
    assert store.get(None) is accounts.NONE
