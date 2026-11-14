"""Notification Center (docs/AGENT-TOOLS.md §24.2, §20.4, §18.11, §22.1, §23): the `notifications` tool reads only the
given apps' notifications (every app's with --dedicated or --phone), newest first, at most 20, and `open` opens one; every
other app's notification appears in no output; a one-time code is read from the setting's SOURCE apps only, within 2
minutes after the code field, once, and is never shown; `/otp` answers with its `source`; the notification trigger fires
once per new notification and never while the phone is in use. Synthetic SpringBoard XML and values only."""
import asyncio
import datetime
import json
import time
from xml.sax.saxutils import quoteattr

import pytest
from fakes import FakeSession
from starlette.testclient import TestClient
from test_daemon import finish, make, run, started
from test_daemon_accounts import BUNDLE, TypingSession, screens, GOES
from test_otp import AUTH, TOKEN, A, prove

from tapwright import center_tool, mapkeeper, notifications as nl, otp, schedulerun, schedules, trace
from tapwright.actionlog import ActionLog
from tapwright.auditlog import AuditLog
from tapwright.control import Refused
from tapwright.daemon import Scope
from tapwright.server import create_app

CHAT = "com.example.chat"  # an installed app that is never given
SMS = "com.apple.MobileSMS"
CODE = "482913"  # the code from the source app (synthetic)
OTHER_CODE = "551177"  # a matching code in a notification of an app that isn't a source
CHAT_SECRET = "chat-only-words-Zq81"
SMS_SECRET = "sms-only-words-Kp42"


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    for name in ("CENTER_OPEN_WAIT", "CENTER_CLOSE_WAIT", "OPEN_WAIT"):
        monkeypatch.setattr(center_tool, name, 0.0)


def cell(app, when, title, body="", y=100, texts=False, name="NotificationShortLookView"):
    pieces = [p for p in (app, when, title, body) if p]
    if texts:
        inner = "".join(f'<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label={quoteattr(p)} '
                        f'x="20" y="{y}" width="300" height="20"/>' for p in pieces)
        return (f'<XCUIElementTypeButton type="XCUIElementTypeButton" name="{name}" label="" x="8" y="{y}" '
                f'width="414" height="80">{inner}</XCUIElementTypeButton>')
    return (f'<XCUIElementTypeButton type="XCUIElementTypeButton" name="{name}" label={quoteattr(", ".join(pieces))} '
            f'x="8" y="{y}" width="414" height="80"/>')


def center(*cells):
    return ('<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" name=" " label=" " '
            'bundleId="com.apple.springboard" x="0" y="0" width="400" height="800">'
            '<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Notification Center" x="0" y="40" '
            'width="400" height="30"/><XCUIElementTypeScrollView type="XCUIElementTypeScrollView" x="0" y="80" '
            f'width="400" height="700">{"".join(cells)}</XCUIElementTypeScrollView></XCUIElementTypeApplication></AppiumAUT>')


INSTALLED = {BUNDLE: "Wayfare", CHAT: "Chat"}
SCREEN = center(
    cell("Chat", "now", "Ana", f"{CHAT_SECRET} Your Wayfare code is {OTHER_CODE}", y=100),
    cell("Wayfare", "2m ago", "Trip booked", "Your trip to Lisbon, Portugal is confirmed", y=190),
    cell("Messages", "now", "+1 555 0100", f"Your Wayfare code is {CODE}. {SMS_SECRET}", y=280),
    cell("Wayfare", "1h ago", "Price drop", y=370),
)


# ---- the parse ---------------------------------------------------------------------------------------------------------
def test_only_kept_apps_come_out_and_an_app_that_cant_be_told_is_dropped():
    got = nl.parse(SCREEN, INSTALLED, lambda b: b == BUNDLE)
    assert [(n.bundle, n.when, n.title) for n in got] == [(BUNDLE, "2m ago", "Trip booked"), (BUNDLE, "1h ago", "Price drop")]
    assert got[0].body == "Your trip to Lisbon, Portugal is confirmed"  # (a body's own ", " is kept)
    assert CHAT_SECRET not in repr(got) and SMS_SECRET not in repr(got)
    # a first piece that is no app's name, or the name two apps share, proves nothing: dropped even when all are kept
    odd = center(cell("Someone", "now", "hi"), cell("Twin", "now", "a"), cell("Wayfare", "now", "kept"))
    got = nl.parse(odd, {**INSTALLED, "com.a.twin": "Twin", "com.b.twin": "Twin"}, lambda b: True)
    assert [n.title for n in got] == ["kept"]
    assert nl.parse("not xml <", INSTALLED, lambda b: True) == []


def test_newest_first_and_at_most_twenty_from_either_shape():
    many = center(*[cell("Wayfare", f"{m}m ago", f"t{m}", y=100 + m) for m in range(30, 0, -1)],
                  cell("Wayfare", "now", "newest", texts=True))
    got = nl.parse(many, INSTALLED, lambda b: True)
    assert len(got) == nl.MAX and got[0].title == "newest" and [n.title for n in got[1:4]] == ["t1", "t2", "t3"]
    assert got[0].age == (0.0, 60.0) and got[1].age == (60.0, 120.0)
    now = datetime.datetime(2026, 11, 14, 10, 0)
    assert nl.age_of("09:30", now) == (1740.0, 1860.0) and nl.age_of("Yesterday", now)[0] == 36000.0
    assert nl.age_of("11/12/26", now) is None


def ios26_cell(app, when, title, body, y=100, more=""):
    """A notification as iOS 26's Notification Center shows it (captured on the phone 2026-11-14 with every text
    replaced): the label is `app[, more], time, title, body`; the Seamless button carries it from the time on; the
    title and the body are their own static texts. `more` is the piece a stack adds after the app's name."""
    rest = ", ".join(p for p in (when, title, body) if p)
    label = ", ".join(p for p in (app, more, rest) if p)
    return (f'<XCUIElementTypeButton type="XCUIElementTypeButton" name="ListCell" label={quoteattr(label)} x="14" '
            f'y="{y}" width="402" height="97"><XCUIElementTypeOther type="XCUIElementTypeOther" '
            f'name="NotificationShortLookView" label={quoteattr(label)} x="14" y="{y}" width="402" height="97">'
            f'<XCUIElementTypeButton type="XCUIElementTypeButton" name="ShortLook.Platter.Content.Seamless" '
            f'label={quoteattr(rest)} x="14" y="{y}" width="402" height="97">'
            f'<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" name="NotificationTitle" '
            f'label={quoteattr(title)} x="78" y="{y + 11}" width="282" height="24"/>'
            f'<XCUIElementTypeOther type="XCUIElementTypeOther" name="NotificationBody.TopAligned.originalMessage" '
            f'x="78" y="{y + 34}" width="321" height="50"><XCUIElementTypeStaticText type="XCUIElementTypeStaticText" '
            f'name="TextContent.Primary" label={quoteattr(body)} x="78" y="{y + 34}" width="321" height="50"/>'
            f'</XCUIElementTypeOther></XCUIElementTypeButton></XCUIElementTypeOther></XCUIElementTypeButton>')


def test_ios26_shape_app_in_any_case_long_times_stacks_and_commas():
    screen = center(
        ios26_cell("WAYFARE", "12 minutes ago", "Trip booked, Lisbon", "Seats 4, 5 and 6, row 2", y=100),
        ios26_cell("Chat", "1 hour ago", "Ana", CHAT_SECRET, y=210),
        ios26_cell("Wayfare", "2 hours ago", "Price drop", "Porto, from 49", y=320, more="2 more"),
        ios26_cell("Somebody Else", "now", "x", "y", y=430),
    )
    got = nl.parse(screen, INSTALLED, lambda b: b == BUNDLE)
    assert [(n.bundle, n.app, n.when, n.title, n.body) for n in got] == [
        (BUNDLE, "Wayfare", "12 minutes ago", "Trip booked, Lisbon", "Seats 4, 5 and 6, row 2"),
        (BUNDLE, "Wayfare", "2 hours ago", "Price drop", "Porto, from 49")]
    assert got[0].age == (720.0, 780.0) and got[1].age == (7200.0, 10800.0)
    assert got[0].center == (215.0, 148.5)
    assert CHAT_SECRET not in repr(got)
    assert [n.app for n in nl.parse(screen, INSTALLED, lambda b: True)][:3] == ["Wayfare", "Chat", "Wayfare"]
    assert nl.age_of("1 hour ago") == (3600.0, 7200.0) and nl.age_of("3 days ago")[0] == 259200.0
    assert nl.age_of("1 week ago") == (604800.0, 1209600.0) and nl.is_time("5 mins ago")


def test_a_code_is_found_only_from_a_source_within_two_minutes_after_the_field_and_once():
    items = nl.parse(SCREEN, {**INSTALLED}, lambda b: True)
    used: set = set()
    now = 10_000.0
    found = nl.find_code(items, (SMS,), "Wayfare", None, now - 30, now, used.__contains__)
    assert found == (CODE, SMS)  # never Chat's, though it matches too
    assert nl.find_code(items, (CHAT,), "nothing like it", None, now - 30, now, used.__contains__) is None
    used.add(CODE)
    assert nl.find_code(items, (SMS,), "Wayfare", None, now - 30, now, used.__contains__) is None  # once
    assert nl.find_code(items, (SMS,), "Wayfare", None, now - 121, now, lambda c: False) is None  # field shown too long ago
    old = nl.parse(center(cell("Messages", "5m ago", "x", f"Wayfare code {CODE}")), {}, lambda b: True)
    assert nl.find_code(old, (SMS,), "Wayfare", None, now - 30, now, lambda c: False) is None  # older than 2 minutes
    before = nl.parse(center(cell("Messages", "1m ago", "x", f"Wayfare code {CODE}")), {}, lambda b: True)
    assert nl.find_code(before, (SMS,), "Wayfare", None, now - 30, now, lambda c: False) is None  # came before the field
    two = nl.parse(center(cell("Messages", "now", "x", "Wayfare code 1234 or 5678")), {}, lambda b: True)
    assert nl.find_code(two, (SMS,), "Wayfare", None, now - 30, now, lambda c: False) is None  # which one? none
    import re
    lettered = nl.parse(center(cell("Messages", "now", "x", "Wayfare key: AB-77Q9")), {}, lambda b: True)
    assert nl.find_code(lettered, (SMS,), "wayfare", re.compile(r"key: AB-(\w{4})"), now - 30, now,
                        lambda c: False) == ("77Q9", SMS)  # the setting's own pattern, its first group
    assert nl.find_code(lettered, (SMS,), "wayfare", re.compile(r"key: AB-(\w{3})"), now - 30, now,
                        lambda c: False) is None  # (3 characters: not a code)
    assert nl.candidates("Wayfare key: AB-77Q9", re.compile(r"key: AB-(\w{4})")) == ["77Q9"]
    assert nl.candidates("code 123 456 now") == ["123456"]


def test_a_code_field_is_told_by_its_words():
    from tapwright.ui import Element
    assert nl.code_like_field(Element("e1", "input", "Verification code", "otp-input"))
    assert not nl.code_like_field(Element("e2", "input", "Email", "email"))


# ---- the tool ----------------------------------------------------------------------------------------------------------
def center_daemon(tmp_path, said, foreground=BUNDLE, source=SCREEN):
    s = FakeSession()
    s.installed = [{"bundle_id": b, "name": n, "version": "1", "build": "1", "executable": n} for b, n in INSTALLED.items()]
    s.foreground, s.app = foreground, (INSTALLED.get(foreground, foreground), foreground)
    s.center_source = source
    d = make(s, audit=AuditLog(tmp_path / "audit"), actions=ActionLog(tmp_path / "log"), log=said.append)
    return d, s


def everything(tmp_path, said, answers, messages) -> str:
    files = [p.read_text("utf-8") for p in tmp_path.rglob("*") if p.is_file() and p.suffix != ".json"]
    return "\n".join([*answers, *map(str, said), *files, *map(json.dumps, messages)])


def drain(queue) -> list:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_personal_mode_reads_only_the_given_apps_and_the_others_are_in_no_output(tmp_path):
    async def go():
        said: list = []
        d, s = center_daemon(tmp_path, said)
        s.center_opens = BUNDLE
        trace.enable(tmp_path / "trace.jsonl")
        queue = d.subscribe()
        answers = []
        try:
            stop, task = await started(d)
            r = await d.run_tool("a", "notifications", {}, scope=Scope((BUNDLE,)))
            answers.append(r["text"])
            r2 = await d.run_tool("a", "notifications", {"open": 2}, scope=Scope((BUNDLE,)))
            answers.append(r2["text"])
            await finish(stop, task)
        finally:
            trace._path = None
        lines = r["text"].splitlines()
        assert lines[0] == "Notifications: 2 from Wayfare (newest first)."
        assert lines[1] == 'n1 Wayfare (com.example.app) 2m ago "Trip booked" "Your trip to Lisbon, Portugal is confirmed"'
        assert lines[2] == 'n2 Wayfare (com.example.app) 1h ago "Price drop"'
        assert "Notification Center was on the phone's screen for about a second" in r["text"]
        assert r["image"] is None and not s.center_open and s.foreground == BUNDLE  # closed, the app back
        assert r2["text"].startswith('Opened n2: n2 Wayfare (com.example.app) 1h ago "Price drop"')
        out = everything(tmp_path, said, answers, drain(queue))
        for secret in (CHAT_SECRET, SMS_SECRET, OTHER_CODE, CODE, "Ana", "+1 555 0100", "Messages", "Chat ("):
            assert secret not in out, secret
        assert '"caption": "Read notifications"' in out
    run(go)


def test_open_takes_a_number_from_the_list_and_says_when_it_isnt_there(tmp_path):
    async def go():
        d, s = center_daemon(tmp_path, [])
        s.center_opens = BUNDLE
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "notifications", {"open": 3}, scope=Scope((BUNDLE,)))
        assert e.value.code == "not_found" and "n3 isn't there" in str(e.value)
        assert not s.center_open and s.foreground == BUNDLE and len(s.tap_points) == 0
        for bad in (0, 21, "1", True):
            with pytest.raises(Refused) as e:
                await d.run_tool("a", "notifications", {"open": bad}, scope=Scope((BUNDLE,)))
            assert e.value.code == "bad_params"
        r = await d.run_tool("a", "notifications", {"open": 1}, scope=Scope((BUNDLE,)))
        assert r["text"].startswith("Opened n1:") and s.tap_points[-1] == (8 + 207, 190 + 40)
        await finish(stop, task)
    run(go)


def test_an_open_that_doesnt_bring_the_app_closes_notification_center_and_says_so(tmp_path):
    """Seen on the phone 2026-11-14: a tap that leaves Notification Center down must not leave it there (the phone locks
    soon after, as on the lock screen)."""
    async def go():
        d, s = center_daemon(tmp_path, [])
        s.center_opens, s.center_tap_stays = BUNDLE, True
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "notifications", {"open": 1}, scope=Scope((BUNDLE,)))
        assert e.value.code == "not_found" and "n1 didn't open" in str(e.value)
        assert not s.center_open and s.foreground == BUNDLE and len(s.tap_points) == 1
        await finish(stop, task)
    run(go)


OPEN_BUTTON = ('<XCUIElementTypeButton type="XCUIElementTypeButton" name="swipe-action-button-identifier" label="Open" '
               'x="14" y="{y}" width="90" height="74"/>')


def test_ios26_the_first_tap_shows_the_rows_open_action_and_a_second_tap_on_it_opens(tmp_path):
    """Seen on the phone 2026-11-14: a tap on a notification shows its row's actions; Open (left, by its id and place,
    never its words) opens the app."""
    async def go():
        d, s = center_daemon(tmp_path, [])
        s.center_opens, s.center_tap_stays = BUNDLE, True
        row_y = 190 + 40  # Wayfare's "Trip booked" row in SCREEN
        s.center_after_tap = SCREEN.replace("</XCUIElementTypeScrollView>", OPEN_BUTTON.format(y=row_y - 30)
                                            + OPEN_BUTTON.replace('x="14"', 'x="320"').format(y=row_y - 30)
                                            + "</XCUIElementTypeScrollView>")
        s.center_open_spot = (14 + 45, row_y - 30 + 37)
        stop, task = await started(d)
        r = await d.run_tool("a", "notifications", {"open": 1}, scope=Scope((BUNDLE,)))
        assert r["text"].startswith("Opened n1:") and s.tap_points == [(8 + 207, row_y), (59, row_y + 7)]
        assert not s.center_open and s.foreground == BUNDLE
        await finish(stop, task)
    run(go)
    assert nl.open_action("not xml", 10, 400) is None
    assert nl.open_action(OPEN_BUTTON.format(y=100), 500, 430) is None  # (another row)
    assert nl.open_action(OPEN_BUTTON.replace('x="14"', 'x="320"').format(y=100), 130, 430) is None  # (the right side)


def test_personal_mode_reads_nothing_while_another_app_is_in_front(tmp_path):
    async def go():
        d, s = center_daemon(tmp_path, [], foreground=CHAT)
        stop, task = await started(d)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "notifications", {}, scope=Scope((BUNDLE,)))
        assert e.value.code == "not_allowed" and e.value.args[0].startswith("outside_apps")
        assert "swipe" not in s.actions() and s.center_reads == 0
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "notifications", {}, scope=Scope(()))
        assert e.value.code == "not_allowed"
        await finish(stop, task)
    run(go)


def test_the_whole_phone_reads_every_app_and_marks_others_words(tmp_path):
    async def go():
        d, s = center_daemon(tmp_path, [])
        stop, task = await started(d)
        r = await d.run_tool("a", "notifications", {})  # scope None: --dedicated or --phone
        await finish(stop, task)
        assert r["text"].splitlines()[0] == "Notifications: 4 from Chat, Messages, Wayfare (newest first)."
        assert '(com.apple.MobileSMS) now (from others) "+1 555 0100"' in r["text"]
    run(go)


def test_the_live_view_pause_stops_the_read(tmp_path):
    async def go():
        d, s = center_daemon(tmp_path, [])
        stop, task = await started(d)
        d.control.set_paused(True)
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "notifications", {}, scope=Scope((BUNDLE,)))
        assert e.value.code == "paused" and s.center_reads == 0
        await finish(stop, task)
    run(go)


# ---- one-time codes ----------------------------------------------------------------------------------------------------
OTP_VAULT = {"version": 2, "vault": {"n": {"kind": "note", "text": "synthetic note"}},
             "otp": {BUNDLE: {"from": [SMS], "match": "Wayfare"}}}


def otp_daemon(tmp_path, data=OTP_VAULT, source=SCREEN):
    path = tmp_path / "private" / "vault.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data), "utf-8")
    s = TypingSession(screens(), "login", GOES)
    s.installed = [{"bundle_id": b, "name": n, "version": "1", "build": "1", "executable": n} for b, n in INSTALLED.items()]
    s.center_source = source
    said: list = []
    d = make(s, audit=AuditLog(tmp_path / "audit"), log=said.append)
    d.maps = mapkeeper.Maps(str(tmp_path / "maps"))
    d.actions = ActionLog(tmp_path / "log")
    d.ceiling = type("C", (), {"accounts": str(path), "brief": False, "maps": None})()
    return d, s, said


def audit_lines(tmp_path):
    return [json.loads(x) for f in sorted((tmp_path / "audit").glob("*.jsonl")) for x in f.read_text("utf-8").splitlines()]


def test_a_code_is_read_from_the_source_app_only_typed_once_and_shown_nowhere(tmp_path):
    async def go():
        d, s, said = otp_daemon(tmp_path)
        queue = d.subscribe()
        trace.enable(tmp_path / "trace.jsonl")
        answers = []
        try:
            stop, task = await started(d)
            answers.append((await d.run_tool("a", "look", {"screenshot": False}))["text"])
            r = await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "${otp." + BUNDLE + "}"})
            answers.append(r["text"])
            assert s.typed[-1][0] == CODE and s.foreground == BUNDLE and not s.center_open
            with pytest.raises(Refused) as e:  # once: the same notification is still there, and gives nothing now
                await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "${otp." + BUNDLE + "}"})
            answers.append(str(e.value))
            assert e.value.code == "not_found" and len([t for t in s.typed if t[0] == CODE]) == 1
            look = await d.run_tool("a", "look", {"screenshot": False})
            answers.append(look["text"])
            assert "<one-time code>" in look["text"]  # (the field shows what was typed)
            whole = await d.run_tool("a", "notifications", {})  # even the whole phone's list hides it
            answers.append(whole["text"])
            assert "Your Wayfare code is <one-time code>." in whole["text"]
            await finish(stop, task)
        finally:
            trace._path = None
        assert [(x["kind"], x.get("app"), x.get("detail")) for x in audit_lines(tmp_path)] == [("otp", BUNDLE, f"from {SMS}")]
        out = everything(tmp_path, said, answers, drain(queue))
        assert CODE not in out and OTHER_CODE not in [t[0] for t in s.typed]
        assert '"caption": "Read a one-time code"' in out
    run(go)


def test_an_old_code_or_one_from_another_app_is_not_taken(tmp_path):
    async def go():
        old = center(cell("Messages", "5m ago", "x", f"Your Wayfare code is {CODE}"),
                     cell("Chat", "now", "Ana", f"Your Wayfare code is {OTHER_CODE}"))
        d, s, _ = otp_daemon(tmp_path, source=old)
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "${otp." + BUNDLE + "}"})
        assert e.value.code == "not_found" and s.typed == [] and s.center_reads == 1
        assert audit_lines(tmp_path) == []
        await finish(stop, task)
    run(go)


def test_codes_are_off_unless_the_setting_turns_them_on(tmp_path):
    async def go():
        d, s, _ = otp_daemon(tmp_path, data={"version": 2, "vault": {"n": {"kind": "note", "text": "synthetic note"}}})
        stop, task = await started(d)
        await d.run_tool("a", "look", {"screenshot": False})
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "type_text", {"target": {"id": "email"}, "text": "${otp." + BUNDLE + "}"})
        assert e.value.code == "bad_params" and "one-time codes are off" in str(e.value) and s.center_reads == 0
        await finish(stop, task)
    run(go)


def test_a_bad_pattern_turns_codes_off_and_says_so(tmp_path):
    from tapwright import accounts
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"version": 2, "vault": {}, "otp": {BUNDLE: {"from": [SMS], "match": "W", "pattern": "("}}}))
    loaded = accounts.load(str(path))
    assert loaded.otp == {} and '"pattern"' in loaded.note
    path.write_text(json.dumps({"version": 2, "vault": {}, "otp": {BUNDLE: {"from": [SMS], "match": "W",
                                                                           "pattern": r"key (\w+)"}}}))
    assert accounts.load(str(path)).otp[BUNDLE].pattern.pattern == r"key (\w+)"


def test_the_code_field_starts_the_two_minutes():
    now = [100.0]
    watch = otp.FieldWatch(clock=lambda: now[0])
    watch.seen(BUNDLE, True)
    now[0] = 130.0
    watch.seen(BUNDLE, True)
    assert watch.shown_at(BUNDLE) == 100.0  # the first read that showed it
    watch.seen(BUNDLE, False)
    assert watch.shown_at(BUNDLE) is None
    codes = otp.Codes(clock=lambda: now[0])
    codes.put(BUNDLE, CODE, SMS, 100.0)
    assert codes.take(BUNDLE).code == CODE and codes.used(CODE)
    codes.put(BUNDLE, CODE, SMS, 100.0)
    assert codes.take(BUNDLE) is None  # handed over once, ever


def handover_daemon(tmp_path, foreground=BUNDLE, setting=True):
    path = tmp_path / "vault.json"
    data = {"version": 2, "vault": {"n": {"kind": "note", "text": "synthetic"}}}
    if setting:
        data["otp"] = {BUNDLE: {"from": [SMS], "match": "Wayfare"}}
    path.write_text(json.dumps(data), "utf-8")
    s = FakeSession()
    s.installed = [{"bundle_id": b, "name": n, "version": "1", "build": "1", "executable": n} for b, n in INSTALLED.items()]
    s.foreground, s.center_source = foreground, SCREEN
    from tapwright.ui import Element
    s.tree, s.tree_app = [Element("e1", "input", "Verification code", "otp-input")], (INSTALLED.get(foreground, foreground), foreground)
    d = make(s, audit=AuditLog(tmp_path / "audit"))
    d._set_state("live")
    d.ceiling = type("C", (), {"accounts": str(path), "brief": False, "maps": None, "apps": None})()
    return create_app(d, TOKEN, hosts={"testserver"}), d, s


def test_otp_hands_tapline_the_code_with_its_source_once(tmp_path):
    app, d, s = handover_daemon(tmp_path)
    c = TestClient(app, client=A)
    prove(c)
    got = c.post("/otp", json={"app": BUNDLE}, headers=AUTH)
    assert got.status_code == 200 and got.json()["code"] == CODE and got.json()["source"] == SMS
    assert got.json()["expires"] > time.time() * 1000 and got.headers["cache-control"] == "no-store"
    assert not s.center_open and s.foreground == BUNDLE
    assert c.post("/otp", json={"app": BUNDLE}, headers=AUTH).status_code == 404  # once
    assert [(x["kind"], x.get("app"), x.get("detail")) for x in audit_lines(tmp_path)] == [("otp", BUNDLE, f"from {SMS}")]
    assert d.scrub_everywhere(f"code {CODE}") == "code <one-time code>"


def test_otp_for_tapline_counts_a_code_field_this_daemon_didnt_see_from_two_minutes_ago(tmp_path):
    """The joint check, 2026-11-14: Tapline's host drove Wayfare to its code field, so this daemon never read it; the code
    came a minute before Tapline asked. The daemon reads the app once: a code field showing counts from 2 minutes ago."""
    app, d, s = handover_daemon(tmp_path)
    s.center_source = center(cell("Messages", "1m ago", "+1 555 0100", f"Your Wayfare code is {CODE}"))
    c = TestClient(app, client=A)
    prove(c)
    got = c.post("/otp", json={"app": BUNDLE}, headers=AUTH)
    assert got.status_code == 200 and got.json()["code"] == CODE
    # no code field on the screen: no code, and Notification Center isn't read
    (tmp_path / "b").mkdir()
    app, d, s = handover_daemon(tmp_path / "b")
    s.tree = []
    c = TestClient(app, client=A)
    prove(c)
    assert c.post("/otp", json={"app": BUNDLE}, headers=AUTH).status_code == 404 and s.center_reads == 0
    # a code older than 2 minutes is still not taken
    (tmp_path / "c").mkdir()
    app, d, s = handover_daemon(tmp_path / "c")
    s.center_source = center(cell("Messages", "3m ago", "+1 555 0100", f"Your Wayfare code is {CODE}"))
    c = TestClient(app, client=A)
    prove(c)
    assert c.post("/otp", json={"app": BUNDLE}, headers=AUTH).status_code == 404


def test_a_code_field_left_open_longer_than_two_minutes_still_takes_a_fresh_code_never_an_old_one(tmp_path):
    """The joint check, 2026-11-14: Verify stayed up 3 minutes after this daemon first saw it; a code that came then
    counts (it is at most 2 minutes old and came after the field showed), an older one never."""
    app, d, s = handover_daemon(tmp_path)
    d.code_fields._at[BUNDLE] = time.time() - 180
    c = TestClient(app, client=A)
    prove(c)
    assert c.post("/otp", json={"app": BUNDLE}, headers=AUTH).status_code == 200  # (SCREEN's code came "now")
    (tmp_path / "old").mkdir()
    app, d, s = handover_daemon(tmp_path / "old")
    d.code_fields._at[BUNDLE] = time.time() - 300
    s.center_source = center(cell("Messages", "4m ago", "+1 555 0100", f"Your Wayfare code is {CODE}"))
    c = TestClient(app, client=A)
    prove(c)
    assert c.post("/otp", json={"app": BUNDLE}, headers=AUTH).status_code == 404


def test_otp_reads_nothing_unless_the_app_is_in_front_and_its_setting_is_on(tmp_path):
    app, d, s = handover_daemon(tmp_path, foreground=CHAT)
    c = TestClient(app, client=A)
    prove(c)
    assert c.post("/otp", json={"app": BUNDLE}, headers=AUTH).status_code == 404 and s.center_reads == 0
    (tmp_path / "off").mkdir()
    app, d, s = handover_daemon(tmp_path / "off", setting=False)
    c = TestClient(app, client=A)
    prove(c)
    assert c.post("/otp", json={"app": BUNDLE}, headers=AUTH).status_code == 404 and s.center_reads == 0


# ---- the notification trigger ------------------------------------------------------------------------------------------
def test_the_trigger_record_fires_once_per_new_notification(tmp_path):
    s = schedules.Schedule("n", "post_update", {}, None, {"notification": BUNDLE})
    where = tmp_path / "sched"
    assert schedules.new_notifications(s, ["a"], where) is False  # the first check only notes what is there
    assert schedules.new_notifications(s, ["a"], where) is False
    assert schedules.new_notifications(s, ["c", "b", "a"], where) is True  # two new: one run now
    assert schedules.new_notifications(s, ["c", "b", "a"], where) is True  # and one at the next check
    assert schedules.new_notifications(s, ["c", "b", "a"], where) is False
    assert schedules.new_notifications(s, ["b"], where) is False  # cleared ones drop out of the record
    assert schedules.new_notifications(s, ["b", "b"], where) is True  # the same words again count again
    assert "post_update" not in (where / "n.seen").read_text() and BUNDLE not in (where / "n.seen").read_text()


class TriggerPhone:
    """The client a scheduled run uses, with Notification Center's digests for the trigger's check."""

    class DaemonError(Exception):
        def __init__(self, code, message="", draft=False):
            super().__init__(message)
            self.code, self.draft = code, draft

    def __init__(self, keys, states=None):
        self.keys, self.states = list(keys), list(states or [{"state": "live", "driver": None, "paused": False}])
        self.calls, self.asked = [], []

    def status(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def notification_keys(self, app):
        self.asked.append(app)
        return self.keys[0] if len(self.keys) == 1 else self.keys.pop(0)

    def set_scope(self, apps, dedicated=False):
        pass

    set_policy = set_accounts = set_maps = lambda self, x=None: None

    def call(self, tool, args, client_id, timeout=120.0, dialog=False):
        self.calls.append(tool)
        return {"text": "Arrived."}


def trigger_deps(tmp_path, phone):
    from test_schedules import Clock, T1, make_route
    from tapwright import pidlock, store
    make_route(tmp_path / "home")
    clock = Clock()
    return schedulerun.Deps(client=phone, sleep=clock.sleep, monotonic=clock.monotonic, where=tmp_path / "sched",
                            runs=tmp_path / "runs", audit_folder=tmp_path / "audit", unlock_phone=None,
                            lock=pidlock.Deps(pid=4242, alive=lambda p: True, start_of=lambda p: T1)), clock


def test_a_notification_trigger_runs_the_route_once_per_new_notification(tmp_path, monkeypatch):
    from tapwright import store
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    phone = TriggerPhone([["a"], ["a"], ["b", "a"], ["b", "a"]])
    deps, _ = trigger_deps(tmp_path, phone)
    schedules.write(schedules.Schedule("n", "post_update", {}, None, {"notification": BUNDLE}), deps.where)
    statuses = [schedulerun.run("n", if_triggered=True, deps=deps).status for _ in range(4)]
    assert statuses == ["idle", "idle", "done", "idle"] and phone.calls == ["drive"] and phone.asked == [BUNDLE] * 4


def test_the_trigger_waits_while_the_phone_is_in_use_and_never_reads_then(tmp_path, monkeypatch):
    from tapwright import store
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    busy = {"state": "live", "driver": "person", "paused": False}
    phone = TriggerPhone([["a"]], states=[busy, busy, {"state": "live", "driver": None, "paused": False}])
    deps, clock = trigger_deps(tmp_path, phone)
    schedules.write(schedules.Schedule("n", "post_update", {}, None, {"notification": BUNDLE}), deps.where)
    assert schedulerun.run("n", if_triggered=True, deps=deps).status == "idle"
    assert clock.slept == [deps.poll, deps.poll] and phone.asked == [BUNDLE]  # read only once it was free
    phone = TriggerPhone([["a"]], states=[{"state": "live", "driver": "agent", "paused": False}])
    deps, clock = trigger_deps(tmp_path, phone)
    assert schedulerun.run("n", if_triggered=True, deps=deps).status == "idle" and phone.asked == []  # never read
    assert sum(clock.slept) >= deps.wait


def test_the_daemons_check_answers_digests_and_refuses_a_busy_phone(tmp_path):
    async def go():
        d, s = center_daemon(tmp_path, [])
        stop, task = await started(d)
        keys = await d.notification_keys(BUNDLE)
        assert len(keys) == 2 and all(len(k) == 24 for k in keys) and not s.center_open
        d.control.set_paused(True)
        with pytest.raises(Refused) as e:
            await d.notification_keys(BUNDLE)
        assert e.value.code == "paused"
        d.control.set_paused(False)
        d.control._person_at = d.control.clock()  # a person just touched the phone
        with pytest.raises(Refused) as e:
            await d.notification_keys(BUNDLE)
        assert e.value.code == "busy" and s.center_reads == 1
        await finish(stop, task)
    run(go)


def test_the_check_route_is_within_the_apps_ceiling(tmp_path):
    from tapwright.ceiling import Ceiling
    _, d, s = handover_daemon(tmp_path)
    app = create_app(d, TOKEN, hosts={"testserver"}, ceiling=Ceiling(apps=(CHAT,)))
    c = TestClient(app, client=A)
    got = c.post("/api/notifications/new", json={"app": BUNDLE}, headers=AUTH)
    assert got.status_code == 403 and s.center_reads == 0
    assert c.post("/api/notifications/new", json={"nope": 1}, headers=AUTH).status_code == 400
    got = c.post("/api/notifications/new", json={"app": CHAT}, headers=AUTH)
    assert got.status_code == 200 and len(got.json()["keys"]) == 1 and CHAT_SECRET not in got.text
    # serve --apps by the app's exact name, as on the phone 2026-11-14 (`--apps Wayfare`): the check sends the bundle id
    by_name = TestClient(create_app(d, TOKEN, hosts={"testserver"}, ceiling=Ceiling(apps=("Chat",))), client=A)
    got = by_name.post("/api/notifications/new", json={"app": CHAT}, headers=AUTH)
    assert "over_ceiling" not in got.text and got.status_code in (200, 409)  # (409: the first check's turn still counts)
    got = by_name.post("/api/notifications/new", json={"app": BUNDLE}, headers=AUTH)
    assert got.status_code == 403 and "over_ceiling" in got.text
    # and every call: a scheduled run names the route's bundle id under `serve --apps <name>`, and the other way round
    for ceiling_apps, call_apps in ((("Chat",), CHAT), ((CHAT,), "Chat")):
        c2 = TestClient(create_app(d, TOKEN, hosts={"testserver"}, ceiling=Ceiling(apps=ceiling_apps)), client=A)
        got = c2.post("/api/apps", json={}, headers={**AUTH, "X-Tapwright-Apps": call_apps})
        assert "over_ceiling" not in got.text, got.text
        got = c2.post("/api/apps", json={}, headers={**AUTH, "X-Tapwright-Apps": f"{call_apps},{BUNDLE}"})
        assert got.status_code == 403 and "over_ceiling" in got.text and BUNDLE in got.text


def test_a_notification_trigger_polls_without_waking_the_pc():
    from test_schedules import Tasks, sched, wake, NS
    s = schedules.Schedule("n", "r", {}, None, {"notification": BUNDLE})
    flag, root = wake(sched().xml(s))
    assert flag == "false" and root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", NS).text == "PT2M"
    assert root.find("t:Actions/t:Exec/t:Arguments", NS).text == "-m tapwright run-schedule n --if-triggered"
    tasks = Tasks()
    said = sched(tasks).install(s)
    assert "Tapwright\\n" in tasks.tasks and f"{BUNDLE}'s notifications every 2 minutes" in said
    assert "never while the phone is in use" in said
