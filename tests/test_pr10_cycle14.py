"""Codex, PR #10 cycle 14: a route's fields other than typed text are literal at the replay as at the save, and account
values are found by decoding the text (percent, JSON and Python escapes, nested, direction marks, whitespace) with a map
back to the original positions, instead of one regular expression per value."""
import json
import random
import time
import urllib.parse
from io import StringIO

import pytest
from test_account_values_raw_inside import BUNDLE, daemon
from test_daemon import finish, make, run, started
from test_pr10_cycle4 import logging_to, with_accounts
from test_pr10_cycle7 import HEADER, pull_with

from tapwright import accounts as accountlib
from tapwright import diagnostics, mapkeeper, routes, trip
from tapwright.control import Refused

USER = "tester2@example.com"


def redaction(*values: str) -> accountlib.Redaction:
    return accountlib.Redaction(tuple((f"t{n}", "password", v) for n, v in enumerate(values)))


# ---- 2: the longest encoded match at a start (the review's trace) ----
NESTED_PW = "abc%252"  # (synthetic, the review's)
NESTED_SHOWN = urllib.parse.quote(NESTED_PW, safe="")  # abc%25252


def test_the_trace_is_what_url_encoding_writes():
    assert NESTED_SHOWN == "abc%25252"


def test_the_whole_encoded_value_is_redacted_and_nothing_of_it_is_left():
    found = redaction(NESTED_PW)
    assert found.scrub(f"token={NESTED_SHOWN};") == "token=<t0's password>;"
    assert found.scrub(f"token={NESTED_PW};") == "token=<t0's password>;"
    assert found.holds(NESTED_SHOWN) and found.holds(NESTED_PW)


def test_the_log_filter_leaves_no_tail_of_the_encoded_value(tmp_path):
    out = StringIO()
    d = with_accounts(make(), tmp_path, password=NESTED_PW)
    log = logging_to(out, d)
    log.info("token=%s;", NESTED_SHOWN)
    text = out.getvalue()
    assert "token=<tester2's password>;" in text
    assert "52;" not in text and "abc%25" not in text


def test_the_crash_export_leaves_no_tail_of_the_encoded_value_and_its_final_check_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)
    body = json.dumps({"token": NESTED_SHOWN})
    answer = pull_with(tmp_path, f"{HEADER}{body}\n".encode(), USER, NESTED_PW)
    path = next((tmp_path / "crashes").rglob("*.ips"))
    raw = path.read_text("utf-8")
    assert f"Saved: {path}" in answer
    assert json.loads(raw.splitlines()[1]) == {"token": "<tester2's password>"}
    assert "abc%25" not in raw


# ---- 3: weak values are refused when the file loads ----
def write(tmp_path, entry: dict) -> str:
    path = tmp_path / "weak.json"
    path.write_text(json.dumps({"version": 1, "accounts": {"tester2": entry}}), "utf-8")
    return str(path)


@pytest.mark.parametrize("field, value", [
    ("password", "   "), ("password", " \t\n "), ("password", "‎‏‎"), ("username", "+++"),
    ("password", "%41%42"), ("password", "\\\\\\\\"), ("username", " ab "),("password", "\\u0041\\u0042"),
])
def test_a_value_too_weak_to_redact_is_refused_at_load_naming_the_account_and_field_never_the_value(tmp_path, field, value):
    loaded = accountlib.load(write(tmp_path, {field: value}), git=lambda args, cwd: None)
    assert loaded.problem is not None and "tester2" in loaded.problem and field in loaded.problem
    assert not value.strip() or value.strip() not in loaded.problem.split(": ", 1)[1]
    assert loaded.names == () and loaded.entries() == ()
    with pytest.raises(Refused):
        loaded.account("tester2", BUNDLE)


def test_a_strong_value_with_spaces_still_loads(tmp_path):
    loaded = accountlib.load(write(tmp_path, {"password": "  pw with spaces  "}), git=lambda args, cwd: None)
    assert loaded.problem is None and loaded.names == ("tester2",)


def test_the_three_space_trace_is_refused_so_a_long_space_run_costs_nothing(tmp_path):
    loaded = accountlib.load(write(tmp_path, {"password": "   "}), git=lambda args, cwd: None)
    assert loaded.problem is not None
    text = "x" + " " * 16000 + "y"
    began = time.perf_counter()
    assert loaded.scrub(text) == text
    assert time.perf_counter() - began < 0.2


# ---- performance ----
def timed(found: accountlib.Redaction, text: str) -> tuple[str, float]:
    began = time.perf_counter()
    out = found.scrub(text)
    return out, time.perf_counter() - began


def test_many_and_overlapping_matches_and_long_runs_stay_fast():
    out, took = timed(redaction("abc "), "abc " * 16000)
    assert took < 0.2 and "abc" not in out
    out, took = timed(redaction("aaaa"), "a" * 20000)
    assert took < 0.2 and out == "<t0's password>"
    out, took = timed(redaction("some-pw-1"), "x" + " " * 16000 + "y")
    assert took < 0.2 and out == "x" + " " * 16000 + "y"
    out, took = timed(redaction("a b c"), "a" + " " * 16000 + "b c")
    assert took < 0.2 and out == "<t0's password>"


def test_a_long_line_of_random_escapes_stays_fast():
    rng = random.Random("cycle14-escapes")
    pieces = ["%25", "%2F", "%41", "%E2%80%8E", "\\u0041", "\\\\", "\\x41", "\\n", "+", " ", "%C3%A9", "\\ud83d\\ude00",
              "\\U0001F600", "\\\"", "%5C", "%", "\\", "a", "b", "‎"]
    text = ""
    while len(text) < 64 * 1024:
        text += rng.choice(pieces)
    found = redaction("abc-secret-1", "tester2@example.com")
    out, took = timed(found, text)
    assert took < 0.2 and out == text
    began = time.perf_counter()
    assert not found.holds(text)
    assert time.perf_counter() - began < 0.2


# ---- property tests: a value encoded per character, nested, with marks and whitespace ----
BIDI = "‎‏‪‫‬‭‮⁦⁧⁨⁩"
VALUES = [
    "synth'part/42", "abc%252", "secret'\"account-42", "pw é 'x\" back\\slash-7", "tab\there now-9",
    "emoji\U0001F600-pw+1", "a&b=c%20d-31", "two\\\\back-slashes", "línea\nnueva-5", "Abc-Secret", "x" * 6,
    "ab ab ab",
]


def _pct(c: str, rng: random.Random) -> str:
    out = "".join(f"%{b:02X}" for b in c.encode("utf-8", "surrogatepass"))
    return out.lower() if rng.random() < 0.5 else out


def one_char(c: str, rng: random.Random) -> str:
    """One character in one of the shapes the matcher decodes."""
    o = ord(c)
    # (a backslash among escapes is written as an escape of its own family: `\\x73` is an escaped backslash then `x73`,
    # as JSON and Python read it, and a `%5c` under a JSON layer is decoded with that layer's `\\`: no encoder writes
    # either, as a percent encoder leaves no backslash and a JSON one no bare one. Codex, PR #10 cycle 15)
    shapes = ["U"] if c == "\\" else ["lit", "pct", "U"]
    shapes += (["u"] if o < 0x10000 else ["pair"]) + (["x"] if o < 0x100 else [])
    if c in "\"'/\\":
        shapes.append("slash")
    if c in "\n\t\r":
        shapes.append("simple")
    if c.isspace():
        shapes += ["plus", "spaces", "nbsp"]
    how = rng.choice(shapes)
    if how == "lit":
        return c
    if how == "pct":
        return _pct(c, rng)
    if how == "U":
        return f"\\U{o:08x}"
    if how == "u":
        return f"\\u{o:04x}" if rng.random() < 0.5 else f"\\u{o:04X}"
    if how == "pair":
        return json.dumps(c)[1:-1]
    if how == "x":
        return f"\\x{o:02x}" if rng.random() < 0.5 else f"\\x{o:02X}"
    if how == "slash":
        return "\\" + c
    if how == "simple":
        return {"\n": "\\n", "\t": "\\t", "\r": "\\r"}[c]
    if how == "plus":
        return "+"
    if how == "nbsp":
        return " "
    return " \t "


def encoded(value: str, rng: random.Random) -> str:
    """The value with each character in its own shape, a direction mark now and then between two, its whitespace in
    any shape, then encoded again whole up to twice more (percent or JSON or repr), so up to 3 levels."""
    out = []
    for n, c in enumerate(value):
        if n and rng.random() < 0.2:
            out.append(rng.choice(BIDI))
        out.append(one_char(c, rng))
    text = "".join(out)
    for _ in range(rng.randint(0, 2)):
        text = rng.choice([
            lambda t: urllib.parse.quote(t, safe=""),
            lambda t: urllib.parse.quote(t),
            lambda t: urllib.parse.quote_plus(t),
            lambda t: json.dumps(t)[1:-1],
            lambda t: json.dumps(t, ensure_ascii=False)[1:-1],
            lambda t: repr(t)[1:-1],
            lambda t: ascii(t)[1:-1],
        ])(text)
    return text


SURROUNDS = ["left [", "] right", "token=", ";", "{\"k\": \"", "\"}", "GET /x?a=1&q=", " 200 OK", "", "\n", "zz "]


@pytest.mark.parametrize("value", VALUES)
def test_every_per_character_encoding_nested_up_to_three_levels_is_held_and_scrubbed_whole(value):
    rng = random.Random(f"cycle14-{value}")
    found = redaction(value, "other-value-77")
    for _ in range(300):
        shown = encoded(value, rng)
        left, right = rng.choice(SURROUNDS), rng.choice(SURROUNDS)
        text = f"{left}{shown}{right}"
        assert found.holds(text), shown
        out = found.scrub(text)
        assert "<t0's password>" in out, (shown, out)
        assert shown not in out, (shown, out)
        # every character of the shape is gone: what is left is only the surroundings (and a quote that went with it)
        rest = out.replace("<t0's password>", "\x00")
        assert rest.count("\x00") == 1, (shown, out)
        a, b = rest.split("\x00")
        assert left.startswith(a) or a.startswith(left.rstrip("\"'")), (shown, out)
        assert right.endswith(b) or b.endswith(right.lstrip("\"'")), (shown, out)


SEAMS = ["", "\\", "%", "%2", "\\u00", "+", "‎", "x", "\\\\", "%25", "'", "\"", "%5C", "\\U0000"]


@pytest.mark.parametrize("value", VALUES)
def test_an_escape_head_beside_the_value_never_hides_it(value):
    # (decoding reads `%2` + `abc` as `%2a` + `bc`, `\` + `tab` as a tab + `ab`, `ab%2` + `F` as `ab/`: the escape that
    # took in the value's first or last characters is redacted with it)
    rng = random.Random(f"cycle14-seams-{value}")
    found = redaction(value)
    for _ in range(300):
        shown = encoded(value, rng)
        text = f"{rng.choice(SEAMS)}{shown}{rng.choice(SEAMS)}"
        assert found.holds(text), text
        out = found.scrub(text)
        assert "<t0's password>" in out and shown not in out, (text, out)


def test_the_seam_traces():
    found = redaction("abc-secret-1")
    assert found.scrub("%2abc-secret-1") == "<t0's password>"  # (`%2a` is `*`: the escape goes with the value)
    assert found.scrub("%2abc%2Dsecret-1") == "<t0's password>"
    accented = redaction("abc-secré-1")
    assert ascii("%2abc-secré-1") == "'%2abc-secr\\xe9-1'"
    assert accented.scrub(ascii("%2abc-secré-1")) == "<t0's password>"  # (a raw `%2`, then ascii() over the whole)
    assert accented.scrub(json.dumps("x %2abc-secré-1")) == "\"x <t0's password>\""
    tab = redaction("tab-secret-9")
    assert tab.scrub("\\t%61b-secret-9") == "<t0's password>"
    tail = redaction("ab-pw%2")
    assert tail.scrub("%61b-pw%2F") == "<t0's password>"
    assert tail.scrub("ab-pw%2F") == "<t0's password>"  # (found as it is, and as `ab-pw` + the escape: merged)
    assert tail.scrub("ab-pw%2G") == "<t0's password>G"  # (no escape there: the value alone)


def test_the_canonical_map_points_each_character_at_its_source():
    text = "x%2525e2%2580%258e%41\\\\u0062\\\"+z"
    canonical, starts, ends = accountlib.canon(text)
    assert canonical == 'xAb" z'
    assert [text[a:b] for a, b in zip(starts, ends)] == \
        ["x", "%41", "\\\\u0062", "\\\"", "+", "z"]  # (the direction mark before A is dropped: no character's source)


# ---- negative: ordinary text isn't over-redacted ----
@pytest.mark.parametrize("value, text", [
    ("synth'part/42", "synth part 42"), ("synth'part/42", "synth'part-42"), ("synth'part/42", "SYNTH'PART/42"),
    ("synth'part/42", "synth'part%2F43"), ("synth'part/42", "s y n t h ' p a r t / 4 2"),
    ("synth'part/42", "synth'part\\n/42"), ("synth'part/42", "synth'part"), ("synth'part/42", "part/42"),
    ("abc%252", "abc%32"), ("abc%252", "abd%252"), ("abc%252", "ab%2"),("abc%252", "abc 252"), ("abc%252", "ABC%252"),
    ("Abc-Secret", "abc-secret"), ("Abc-Secret", "Abc-Secre"), ("aaaa", "aaa baaa"),
    ("pw with spaces", "pwwithspaces"), ("pw with spaces", "pw-with-spaces"),
    ("tester2@example.com", "tester2@example.co"), ("tester2@example.com", "the tester at example.com"),
])
def test_ordinary_text_that_shares_characters_is_left_alone(value, text):
    found = redaction(value)
    assert not found.holds(text), text
    assert found.scrub(text) == text


def test_a_placeholder_already_there_is_left_alone():
    tester = accountlib.Redaction((("tester", "password", "test"),))
    once = tester.scrub("a test b %74%65%73%74")
    assert once == "a <tester's password> b <tester's password>"
    assert tester.scrub(once) == once


# ---- 1: a route's selector is literal at the replay as at the save ----
TYPED_LIKE = "hi $${fresh_login42}"  # (the review's: a button's label, as the screen shows it)
NEW_PASSWORD = "hi ${fresh_login42}"  # (what the account's password is changed to before the stop)


def test_substitute_fills_declared_variables_and_decodes_in_every_field():
    steps = [{"tool": "tap", "target": {"label": TYPED_LIKE}},
             {"tool": "wait_for", "text": "$${x} ${v}"},
             {"tool": "type_text", "text": "a $${b} ${v}"}]
    got = routes.substitute(steps, {"v": "V"}, ["v"])
    assert got[0]["target"]["label"] == NEW_PASSWORD and got[1]["text"] == "${x} V"
    assert got[2]["text"] == "a ${b} V"


def test_a_literal_field_may_hold_an_undeclared_dollar_brace_and_an_account_placeholder_there_is_still_refused():
    route = routes.route("r", BUNDLE, "profile", None, [], [{"tool": "tap", "target": {"label": "a ${undeclared} b"}}])
    routes.check(route)  # (undeclared: literal text, nothing to declare)
    routes.check(routes.route("r", BUNDLE, "profile", None, [], [{"tool": "tap", "target": {"label": "a $${account.x}"}}]))
    with pytest.raises(Refused) as e:
        routes.check(routes.route("r", BUNDLE, "profile", None, ["account"],
                                  [{"tool": "tap", "target": {"label": "${account.password}"}}]))
    assert e.value.code == "bad_params" and "type_text" in str(e.value)


def test_the_review_trace_saves_the_label_written_twice_and_replay_taps_the_recorded_button(tmp_path, monkeypatch):
    async def no_drive(self):
        return None
    monkeypatch.setattr(trip.Trip, "reach", no_drive)
    out = {}

    async def go():
        d = daemon(tmp_path, password="pw-secret-0042")
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        d.ceiling.maps = str(tmp_path / "maps")
        d.session.buttons = [("", TYPED_LIKE), ("", NEW_PASSWORD)]

        async def place(scope):
            return BUNDLE, "profile"
        d._route_place = place
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "route": "literal_selector"})
        await d.run_tool("a", "tap", {"target": {"label": TYPED_LIKE}, "look": False})
        recorded_at = d.session.tap_points[-1]
        path = d.ceiling.accounts
        data = json.loads(open(path, encoding="utf-8").read())
        data["accounts"]["tester2"]["password"] = NEW_PASSWORD
        open(path, "w", encoding="utf-8").write(json.dumps(data, indent=2) + "\n")
        await d.run_tool("a", "look", {"screenshot": False})  # loads the changed file
        out["said"] = (await d.run_tool("a", "record", {"stop": True}))["text"]
        saved = next((tmp_path / "maps").rglob("literal_selector.json"))
        out["raw"] = saved.read_text("utf-8")
        before = len(d.session.tap_points)
        out["replayed"] = (await d.run_tool("a", "replay", {"path": str(saved)}))["text"]
        out["taps"] = d.session.tap_points[before:]
        out["recorded_at"] = recorded_at
        await finish(stop, task)
    run(go)
    assert out["said"].startswith("Saved route literal_selector"), out["said"]
    saved = json.loads(out["raw"])
    assert saved["steps"] == [{"tool": "tap", "target": {"label": TYPED_LIKE.replace("${", "$${")}}]  # (written twice)
    assert NEW_PASSWORD not in out["raw"]
    assert out["replayed"].startswith("Ran 1 of 1 steps"), out["replayed"]
    assert out["taps"][-1] == out["recorded_at"]  # the recorded button, not the one labelled with the password


def test_a_label_that_is_the_new_password_refuses_the_save(tmp_path, monkeypatch):
    async def no_drive(self):
        return None
    monkeypatch.setattr(trip.Trip, "reach", no_drive)
    out = {}

    async def go():
        d = daemon(tmp_path, password="pw-secret-0042")
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        d.ceiling.maps = str(tmp_path / "maps")
        d.session.buttons = [("", NEW_PASSWORD), ("", TYPED_LIKE)]

        async def place(scope):
            return BUNDLE, "profile"
        d._route_place = place
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "route": "literal_selector"})
        await d.run_tool("a", "tap", {"target": {"label": NEW_PASSWORD}, "look": False})
        path = d.ceiling.accounts
        data = json.loads(open(path, encoding="utf-8").read())
        data["accounts"]["tester2"]["password"] = NEW_PASSWORD
        open(path, "w", encoding="utf-8").write(json.dumps(data, indent=2) + "\n")
        await d.run_tool("a", "look", {"screenshot": False})
        try:
            await d.run_tool("a", "record", {"stop": True})
        except Refused as e:
            out["refused"] = str(e)
        out["files"] = [str(f) for f in (tmp_path / "maps").rglob("literal_selector.json")]
        await finish(stop, task)
    run(go)
    assert "step 1" in out["refused"] and NEW_PASSWORD not in out["refused"], out
    assert out["files"] == []


def test_a_version_1_script_has_no_templates_and_nothing_is_decoded():
    from tapwright import batch
    assert batch.literal_runs("a $${b} ${account.username}")[0] == ["a $${b} ", ""]
    out = batch.redacted_content(batch.script("s", None, [{"tool": "tap", "target": {"label": "a $${b} ${c}"}}]),
                                 lambda t: t.replace("${c}", "<x>"))
    assert out["steps"][0]["target"]["label"] == "a $${b} <x>"


def test_a_route_redaction_reads_a_field_decoded():
    from tapwright import batch
    route = routes.route("r", BUNDLE, "profile", None, [], [{"tool": "tap", "target": {"label": "hi $${pw}"}}])
    out = batch.redacted_content(route, lambda t: t.replace("hi ${pw}", "<p>"))
    assert out["steps"][0]["target"]["label"] == "<p>"
