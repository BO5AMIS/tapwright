"""A revealed password or a username with a quote, a backslash, a run of spaces or a long one is redacted before it is
escaped, collapsed or cut, so it is in no text answer in any form (AGENT-TOOLS §18.6; Codex, PR #10)."""
import json

import pytest
from test_daemon import finish, run, started
from test_daemon_accounts import BUNDLE, mapped, write_accounts

from tapwright import accounts as accountlib
from tapwright import ui
from tapwright.ui import TEXT_LIMIT

LONG = "Lg" + "pw-abcdef0123" * 19  # (longer than a cut, within the 256 a value may have: Codex, PR #10 cycle 15)
VALUES = {"quote": 'synth"password42', "backslash": "synth\\password42", "spaces": "synth  password  42",
          "newline": "synth\npassword42", "long": LONG, "both": 'a"b\\c  d'}
assert TEXT_LIMIT < len(LONG) <= 256


def forms(value):
    """Every way the value could reach an answer: as it is, collapsed, escaped, cut."""
    flat = " ".join(value.split())
    return {value, flat, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(flat, ensure_ascii=False)[1:-1],
            flat[:TEXT_LIMIT], value[:TEXT_LIMIT - 4]}


def account_with(d, tmp_path, password, username="tester2@example.com"):
    data = {"version": 1, "accounts": {"tester2": {"username": username, "password": password, "apps": [BUNDLE]}}}
    d.ceiling.accounts = write_accounts(tmp_path, data, "revealed.json")


@pytest.mark.parametrize("kind", sorted(VALUES))
def test_a_revealed_password_is_in_no_text_answer_in_any_form(tmp_path, kind):
    value = VALUES[kind]

    async def go():
        d = mapped(tmp_path)
        account_with(d, tmp_path, value)
        d.session.at = "login"
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.values["email"] = value  # the show-password toggle made the field plain text
        looked = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        said = [looked]
        d.session.values.pop("email")
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.values["email"] = value
        said.append((await d.run_tool("a", "tap", {"target": {"id": "l1"}}))["text"])  # the change list: `~` a changed value
        said.append((await d.run_tool("a", "wait_for", {"id": "email"}))["text"])
        said.append((await d.run_tool("a", "run", {"steps": [{"tool": "tap", "target": {"id": "l1"}}]}))["text"])
        assert "<tester2's password>" in looked
        for text in said:
            for form in forms(value):
                if len(form) >= accountlib.MIN_SCRUBBED + 4:
                    assert form not in text, (kind, form, text)
        await finish(stop, task)
    run(go)


def test_a_username_with_a_quote_in_a_plain_field_is_redacted(tmp_path):
    name = 'te"ster\\two@example.com'

    async def go():
        d = mapped(tmp_path)
        account_with(d, tmp_path, "unused-pw-1234", username=name)
        d.session.at = "login"
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        d.session.values["email"] = name
        said = (await d.run_tool("a", "tap", {"target": {"id": "l1"}}))["text"]
        looked = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        for text in (said, looked):
            assert "<tester2's username>" in text
            for form in forms(name):
                assert form not in text
        await finish(stop, task)
    run(go)


def test_the_ui_helpers_redact_before_escaping_and_cutting():
    loaded = accountlib.Loaded(path="x", accounts={"a": accountlib.Account("a", "u@example.com", 'p"w  ' + "z" * 200)})
    token = ui.REDACT.set(loaded.scrub)
    try:
        assert ui.clip('value p"w  ' + "z" * 200) == '"value <a\'s password>"'
        assert ui.quoted('p"w  ' + "z" * 200) == '"<a\'s password>"'
    finally:
        ui.REDACT.reset(token)


def test_scrub_holds_and_clean_catch_the_escaped_and_the_collapsed_forms():
    value = 'p"w\\ x  y'
    loaded = accountlib.Loaded(path="x", accounts={"a": accountlib.Account("a", None, value)})
    for form in forms(value) - {value[:TEXT_LIMIT - 4]}:
        assert loaded.holds(f"name {form} end"), form
        assert loaded.scrub(f"name {form} end") == "name <a's password> end"
    assert loaded.clean({"k": f'"{json.dumps(value)[1:-1]}"', "ok": "Sign in"}) == {"ok": "Sign in"}
