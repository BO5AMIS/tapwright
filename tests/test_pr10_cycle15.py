"""Codex, PR #10 cycle 15: JSON's \\b and \\f, escapes that take in both ends of one value, decoding to convergence with a
work limit that fails closed, literal backslashes, a route stop that fails, long values, and undeclared variables in
typed text."""
from fakes import bare
import json
import random
import time
import urllib.parse
from io import StringIO

import pytest
from test_account_values_raw_inside import BUNDLE, daemon
from test_daemon import finish, make, run, started
from test_daemon_routes import explore, folder, mapped
from test_pr10_cycle4 import logging_to, with_accounts
from test_pr10_cycle7 import HEADER, pull_with
from test_pr10_cycle14 import one_char

from tapwright import accounts as accountlib
from tapwright import batch, diagnostics, mapkeeper, routes, trip
from tapwright.control import Refused

USER = "tester2@example.com"
B = "\\"  # (one backslash)


def redaction(*values: str) -> accountlib.Redaction:
    return accountlib.Redaction(tuple((f"t{n}", "password", v) for n, v in enumerate(values)))


def logged(tmp_path, password: str, line: str) -> str:
    out = StringIO()
    d = with_accounts(make(), tmp_path, password=password)
    logging_to(out, d).info("%s", line)
    return out.getvalue()


def exported(tmp_path, monkeypatch, password: str, body: str) -> tuple[str, str]:
    monkeypatch.setattr(diagnostics, "crash_folder", lambda bundle: tmp_path / "crashes" / bundle)
    answer = pull_with(tmp_path, f"{HEADER}{body}\n".encode(), USER, password)
    paths = list((tmp_path / "crashes").rglob("*.ips"))
    return answer, paths[0].read_text("utf-8") if paths else ""


# ---- 1: JSON's \b and \f ----
CONTROL = ["abc\bdef", "abc\fdef"]  # (synthetic, the review's)


@pytest.mark.parametrize("password", CONTROL)
def test_backspace_and_form_feed_escapes_are_decoded(password):
    found = redaction(password)
    for shown in (json.dumps(password), json.dumps(json.dumps(password)), repr(password), json.dumps({"token": password})):
        assert found.holds(shown), shown
        assert "abc" not in found.scrub(shown) and "def" not in found.scrub(shown), shown


@pytest.mark.parametrize("password", CONTROL)
@pytest.mark.parametrize("nested", [False, True])
def test_the_log_filter_redacts_a_backspace_or_form_feed_value(tmp_path, password, nested):
    line = json.dumps({"token": json.dumps(password) if nested else password})
    text = logged(tmp_path, password, line)
    assert "<tester2's password>" in text and "abc" not in text and "def" not in text, text


@pytest.mark.parametrize("password", CONTROL)
@pytest.mark.parametrize("nested", [False, True])
def test_the_crash_export_redacts_a_backspace_or_form_feed_value(tmp_path, monkeypatch, password, nested):
    body = json.dumps({"token": json.dumps(password) if nested else password})
    answer, raw = exported(tmp_path, monkeypatch, password, body)
    assert "Saved: " in answer, answer
    token = json.loads(raw.splitlines()[1])["token"]
    assert "abc" not in raw and "def" not in raw and "<tester2's password>" in token, raw


# ---- 2: escapes taking in both ends of one value ----
BOTH = "abc%2"  # (synthetic, the review's)
BOTH_SHOWN = "%2a%62c%2F"  # `%2` + the value with `b` encoded + `F`


def test_the_review_trace_both_ends_taken_in():
    assert "%2" + "a%62c%2" + "F" == BOTH_SHOWN
    found = redaction(BOTH)
    assert found.holds(BOTH_SHOWN)
    assert found.scrub(f"token={BOTH_SHOWN};") == "token=<t0's password>;"


def test_the_log_filter_and_the_crash_export_redact_both_ends_taken_in(tmp_path, monkeypatch):
    (tmp_path / "log").mkdir()
    text = logged(tmp_path / "log", BOTH, f"token={BOTH_SHOWN};")
    assert "token=<tester2's password>;" in text and "%62" not in text, text
    answer, raw = exported(tmp_path, monkeypatch, BOTH, json.dumps({"token": BOTH_SHOWN}))
    assert "Saved: " in answer, answer
    assert json.loads(raw.splitlines()[1]) == {"token": "<tester2's password>"}


# (value, heads: what before it takes in its first characters, how many; tails: what after it takes in its last ones)
EDGES = [
    ("abc%2", [("%2", 1), ("%", 2)], [("F", 2), ("0", 2), ("2F", 1)]),
    ("bad-pw" + B, [(B, 1), ("%", 2), (B + "u00", 2)], [("n", 1), ("t", 1), ("u0041", 1)]),
    ("f00-secret" + B + "u00", [(B, 1), ("%", 2), (B + "x", 2)], [("41", 4), ("e9", 4)]),
    ("e9-pw-" + B + "x4", [("%", 2), (B + "u00", 2), (B + "x", 2)], [("1", 3), ("F", 3)]),
    ("t-pass-%", [(B, 1)], [("41", 1), ("2F", 1), ("e2%80%8e", 1)]),
    ("4a-secret-%2", [(B + "u00", 2), ("%", 2), (B + "x", 2)], [("F", 2), ("0", 2)]),
]


def middle(text: str, rng: random.Random) -> str:
    """Each character in its own shape, but one that can't join the escape before or after it (a letter kept)."""
    return "".join(one_char(c, rng) if c not in "%\\" else c for c in text)


@pytest.mark.parametrize("value, heads, tails", EDGES)
def test_random_escapes_at_both_boundaries_never_hide_the_value(value, heads, tails):
    rng = random.Random(f"cycle15-edges-{value}")
    found = redaction(value, "other-value-77")
    for _ in range(300):
        (head, k1), (tail, k2) = rng.choice(heads), rng.choice(tails)
        shown = head + value[:k1] + middle(value[k1:len(value) - k2], rng) + value[len(value) - k2:] + tail
        if rng.random() < 0.5:
            shown = rng.choice([lambda t: urllib.parse.quote(t, safe=""), lambda t: json.dumps(t)[1:-1],
                                lambda t: repr(t)[1:-1]])(shown)
        left, right = rng.choice(["", "token=", "x ", "[", "{\"k\": \""]), rng.choice(["", ";", " y", "]", "\"}"])
        text = left + shown + right
        assert found.holds(text), text
        out = found.scrub(text)
        assert "<t0's password>" in out and shown not in out, (text, out)
        rest = out.replace("<t0's password>", "\x00")
        assert rest.count("\x00") == 1, (text, out)
        a, b = rest.split("\x00")
        # all of the shape went, escapes and all (and a space that an escape's space was merged with): what is left is
        # the surroundings, and of the text that took in an edge only what isn't the value's (`%` of `%` + `ab`, or
        # `F` of `2F` when `%2` + `2` made the escape)
        assert a.startswith(left.rstrip("\"' ")) and len(a) - len(left) < 6, (text, out)
        assert b.endswith(right.lstrip("\"' ")) and len(b) - len(right) < 9, (text, out)


def test_one_side_taken_in_still_works_and_ordinary_text_is_left():
    found = redaction(BOTH)
    assert found.scrub("%2abc%2") == "<t0's password>"
    assert found.scrub("abc%2F") == "<t0's password>"
    for text in ("%2a%62d%2F", "%2b%62c%2F", "bc%2F", "%2a%62c"):
        assert not found.holds(text) and found.scrub(text) == text, text


# ---- 3: decoding to convergence, failing closed at the work limit ----
SLASHED = "pw/secret42"  # (synthetic, the review's)


def quoted(value: str, times: int) -> str:
    for _ in range(times):
        value = urllib.parse.quote(value, safe="")
    return value


def test_the_seven_layer_trace_is_found():
    shown = quoted(SLASHED, 7)
    assert shown == "pw%2525252525252Fsecret42"
    found = redaction(SLASHED)
    assert found.holds(shown) and found.scrub(f"token={shown};") == "token=<t0's password>;"


def test_the_seven_layer_trace_in_the_log_filter_and_the_crash_export(tmp_path, monkeypatch):
    shown = quoted(SLASHED, 7)
    (tmp_path / "log").mkdir()
    text = logged(tmp_path / "log", SLASHED, f"token={shown};")
    assert "token=<tester2's password>;" in text and "secret42" not in text, text
    answer, raw = exported(tmp_path, monkeypatch, SLASHED, json.dumps({"token": shown}))
    assert "Saved: " in answer and json.loads(raw.splitlines()[1]) == {"token": "<tester2's password>"}


@pytest.mark.parametrize("times", [16, 17])  # (the work limit, and one past it)
def test_up_to_the_work_limit_the_value_is_found_and_at_it_too(times):
    assert accountlib.MAX_PASSES == 16
    shown = quoted(SLASHED, times)
    found = redaction(SLASHED)
    assert found.holds(shown)
    out = found.scrub(f"a {shown} b")
    assert out.startswith("a <") and out.endswith("> b") and "secret42" not in out, out


def test_far_beyond_the_work_limit_the_whole_token_is_redacted_and_held():
    shown = quoted(SLASHED, 40)
    found = redaction(SLASHED)
    assert found.holds(shown)
    text = f"before {shown}; after"
    out = found.scrub(text)
    assert out == f"before {accountlib.UNCHECKED} after", out
    assert found.scrub(out) == out and found.holds(out)  # (idempotent, and the placeholder is held like any other)
    assert found.holds(f"x={shown}") and found.scrub(f"x={shown}") == accountlib.UNCHECKED


def test_far_beyond_the_work_limit_in_the_log_filter_and_the_crash_export(tmp_path, monkeypatch):
    shown = quoted(SLASHED, 40)
    (tmp_path / "log").mkdir()
    text = logged(tmp_path / "log", SLASHED, f"token {shown} end")
    assert f"token {accountlib.UNCHECKED} end" in text and "secret42" not in text, text
    answer, raw = exported(tmp_path, monkeypatch, SLASHED, json.dumps({"token": shown}))
    assert "Saved: " in answer and json.loads(raw.splitlines()[1]) == {"token": accountlib.UNCHECKED}, (answer, raw)


def test_without_values_nothing_is_redacted_however_deep():
    shown = quoted(SLASHED, 40)
    assert accountlib.Redaction().scrub(shown) == shown and not accountlib.Redaction().holds(shown)


def test_a_value_that_does_not_settle_within_the_limit_is_refused_at_load(tmp_path):
    loaded = accountlib.load(write(tmp_path, {"password": quoted("pw/x", accountlib.MAX_PASSES + 1)}),
                             git=lambda args, cwd: None)
    assert loaded.problem is not None and "tester2's password" in loaded.problem and loaded.names == ()


# ---- 4: literal backslashes ----
LITERAL = "pass" + B + "word"  # (synthetic, the review's: r"pass\word")


def test_a_literal_backslash_is_kept_on_both_sides():
    found = redaction(LITERAL)
    assert not found.holds("password") and found.scrub("the password field") == "the password field"
    assert not found.holds("pass word") and not found.holds("pass" + B + B + "Word")
    for shown in (LITERAL, json.dumps(LITERAL), repr(LITERAL), json.dumps(json.dumps(LITERAL)),
                  urllib.parse.quote(LITERAL), "pass" + B + "u005cword"):
        assert found.holds(shown), shown
        rest = found.scrub(f"[{shown}]").replace("<t0's password>", "")
        assert "pass" not in rest and "word" not in rest, (shown, rest)
    plain = redaction("password")
    assert not plain.holds(LITERAL) and not plain.holds("pass" + B + B + "word")


def test_the_review_trace_a_plain_word_password_is_saved(tmp_path):
    out = {}

    async def go():
        d = daemon(tmp_path, password=LITERAL)
        d.session.buttons = [("info", "password"), ("other", "Keep")]
        stop, task = await started(d)
        out["look"] = (await d.run_tool("a", "look", {"screenshot": False}))["text"]
        await d.run_tool("a", "record", {"start": True, "name": "plain_word"})
        await d.run_tool("a", "tap", {"target": {"id": "info"}, "look": False})
        await d.run_tool("a", "wait_for", {"text": "password"})
        out["said"] = (await d.run_tool("a", "record", {"stop": True, "path": str(tmp_path / "plain.json")}))["text"]
        out["saved"] = json.loads((tmp_path / "plain.json").read_text("utf-8"))
        await finish(stop, task)
    run(go)
    assert "password" in out["look"] and "<tester2's password>" not in out["look"], out["look"]
    assert out["said"].startswith("Saved 1 steps"), out["said"]  # (the wait is the tap's expectation)
    assert bare(out["saved"]["steps"]) == [{"tool": "tap", "target": {"id": "info"}, "expect": {"text": "password"}}]


# ---- 5: a route stop that fails changes nothing ----
LABEL = "hi ${x}"  # (synthetic, the review's)


def test_a_failed_route_stop_then_a_good_one_escapes_once_and_replay_taps_the_recorded_button(tmp_path, monkeypatch):
    async def no_drive(self):
        return None
    monkeypatch.setattr(trip.Trip, "reach", no_drive)
    out = {}

    async def go():
        d = daemon(tmp_path)
        d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
        d.ceiling.maps = str(tmp_path / "maps")
        d.session.buttons = [("", LABEL), ("", "hi $${x}")]
        calls = []

        async def place(scope):
            calls.append(1)
            if len(calls) == 2:  # the first stop: a screen with nothing to recognise it by
                raise Refused("bad_params", "this screen has nothing to recognise it by")
            return BUNDLE, "profile"
        d._route_place = place
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        await d.run_tool("a", "record", {"start": True, "route": "dollar_label"})
        await d.run_tool("a", "tap", {"target": {"label": LABEL}, "look": False})
        out["recorded_at"] = d.session.tap_points[-1]
        with pytest.raises(Refused):
            await d.run_tool("a", "record", {"stop": True})
        out["files_after_failure"] = list((tmp_path / "maps").rglob("dollar_label.json"))
        out["said"] = (await d.run_tool("a", "record", {"stop": True}))["text"]
        saved = next((tmp_path / "maps").rglob("dollar_label.json"))
        out["saved"] = json.loads(saved.read_text("utf-8"))
        before = len(d.session.tap_points)
        out["replayed"] = (await d.run_tool("a", "replay", {"path": str(saved)}))["text"]
        out["taps"] = d.session.tap_points[before:]
        await finish(stop, task)
    run(go)
    assert out["files_after_failure"] == []
    assert out["said"].startswith("Saved route dollar_label"), out["said"]
    assert bare(out["saved"]["steps"]) == [{"tool": "tap", "target": {"label": "hi $${x}"}}]  # (escaped once)
    assert out["replayed"].startswith("Ran 1 of 1 steps"), out["replayed"]
    assert out["taps"] == [out["recorded_at"]]


# ---- 6: long values ----
def write(tmp_path, entry: dict) -> str:
    path = tmp_path / "long.json"
    path.write_text(json.dumps({"version": 1, "accounts": {"tester2": entry}}), "utf-8")
    return str(path)


@pytest.mark.parametrize("field", ["username", "password"])
@pytest.mark.parametrize("length", [257, 8000, 64000])
def test_a_value_longer_than_256_is_refused_at_load_naming_the_account_and_field_never_the_value(tmp_path, field, length):
    value = "a" * (length - 4) + "wxyz"
    loaded = accountlib.load(write(tmp_path, {field: value}), git=lambda args, cwd: None)
    assert loaded.problem is not None and f"tester2's {field}" in loaded.problem and "256" in loaded.problem
    assert "wxyz" not in loaded.problem and loaded.names == () and loaded.entries() == ()


def test_a_256_character_value_loads(tmp_path):
    loaded = accountlib.load(write(tmp_path, {"password": "p" * 252 + "-256"}), git=lambda args, cwd: None)
    assert loaded.problem is None and loaded.names == ("tester2",)


def timed(found: accountlib.Redaction, text: str) -> tuple[str, float]:
    began = time.perf_counter()
    out = found.scrub(text)
    return out, time.perf_counter() - began


def test_a_256_character_value_in_a_128_kb_message_stays_fast():
    out, took = timed(redaction("a" * 256), "a" * 128000)
    assert took < 0.2 and out == "<t0's password>", took
    out, took = timed(redaction("a" * 256), ("a" * 255 + "b") * 500)
    assert took < 0.2 and out == ("a" * 255 + "b") * 500, took
    rng = random.Random("cycle15-long")
    value = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789-_") for _ in range(256))
    words = 'GET /api/v1/items?id=42&q=hello%20world {"user": "tester", "ok": true} the quick brown fox '
    text = (words * (128000 // len(words) + 1))[:128000]
    text = text[:1000] + value + text[1000:90000] + urllib.parse.quote(json.dumps(value)) + text[90000:]
    out, took = timed(redaction(value), text)
    assert took < 0.2 and value not in out and out.count("<t0's password>") == 2, took
    # (a 64 KB line of nothing but escapes and a 256-character value of escape characters, both ways: the worst kind)
    value = "".join(rng.choice("abcdef%\\-") for _ in range(256))
    text = "".join(rng.choice("abcdef%\\- ") for _ in range(64000))
    text = text[:5000] + value + text[5000:40000] + json.dumps(value) + text[40000:]
    out, took = timed(redaction(value), text)
    assert took < 0.2 and value not in out and out.count("<t0's password>") >= 2, took


@pytest.mark.parametrize("hay, needle", [("aaaa", "aa"), ("abababa", "aba"), ("aabaabaa", "aabaa"), ("xyz", "y"),
                                         ("abcabcabc", "abcabc"), ("aaabaaab", "aab")])
def test_overlapping_occurrences_match_a_naive_search(hay, needle):
    naive = [(i, i + len(needle)) for i in range(len(hay)) if hay.startswith(needle, i)]
    assert list(accountlib._occurrences(hay, needle, accountlib._failure(needle), False)) == naive


def test_overlapping_occurrences_match_a_naive_search_at_random():
    rng = random.Random("cycle15-kmp")
    for _ in range(3000):
        needle = "".join(rng.choice("ab") for _ in range(rng.randint(1, 6)))
        hay = "".join(rng.choice("ab") for _ in range(rng.randint(0, 40)))
        naive = [(i, i + len(needle)) for i in range(len(hay)) if hay.startswith(needle, i)]
        fail = accountlib._failure(needle)
        assert list(accountlib._occurrences(hay, needle, fail, False)) == naive, (hay, needle)
        merged: list[tuple[int, int]] = []
        for a, b in naive:
            if merged and a < merged[-1][1]:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        assert list(accountlib._occurrences(hay, needle, fail, True)) == merged, (hay, needle)


# ---- 7: an undeclared variable is literal in typed text too ----
def test_an_undeclared_variable_in_typed_text_is_accepted_and_left_literal():
    route = routes.route("r", BUNDLE, "profile", None, [], [{"tool": "type_text", "text": "hello ${x}"}])
    batch.check_script(route, "r.json")  # (the review's trace: refused before)
    assert routes.substitute(route["steps"], {}, [])[0]["text"] == "hello ${x}"
    declared = routes.route("r", BUNDLE, "profile", None, ["y"], [{"tool": "type_text", "text": "hello ${x} ${y}"}])
    batch.check_script(declared, "r.json")
    assert routes.substitute(declared["steps"], {"x": "X", "y": "Y"}, ["y"])[0]["text"] == "hello ${x} Y"


@pytest.mark.parametrize("text, variables", [
    ("hi ${account.password}", ["account"]), ("${account.password}", []), ("${account.token}", ["account"]),
])
def test_the_account_rules_in_typed_text_are_kept(text, variables):
    with pytest.raises(Refused) as e:
        batch.check_script(routes.route("r", BUNDLE, "profile", None, variables, [{"tool": "type_text", "text": text}]),
                           "r.json")
    assert e.value.code == "bad_params"


def test_replay_types_an_undeclared_variable_literally(tmp_path):
    async def go():
        d = mapped(tmp_path)
        stop, task = await started(d)
        await explore(d)
        path = folder(tmp_path) / "routes" / "typed.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(routes.route("typed", BUNDLE, "welcome", "login", [], [
            {"tool": "tap", "target": {"id": "continue-btn"}},
            {"tool": "type_text", "text": "hello ${x}", "target": {"id": "email"}}])), "utf-8")
        d.session.at = "welcome"
        r = await d.run_tool("a", "replay", {"path": str(path)})
        assert r["text"].startswith("Ran 2 of 2 steps"), r["text"]
        assert d.session.typed[-1][0] == "hello ${x}"
        await finish(stop, task)
    run(go)
