"""Grants (docs/AGENT-TOOLS.md §19, 1.14.2): the policy file v2, what a grant covers, the use slots, the edit lock and
the kill switch, and the audit line every use needs (§23). Synthetic data only; real files in tmp_path."""
import datetime
import json
import multiprocessing
import os
import threading
import time

import pytest

from tapwright import grants, pidlock, policy
from tapwright.auditlog import AuditLog

TZ = datetime.timezone(datetime.timedelta(hours=3))
APP = "com.example.wayfare"


class Clock:
    """A local clock the test moves."""

    def __init__(self, at="2026-11-14T10:00:00"):
        self.at = datetime.datetime.fromisoformat(at).replace(tzinfo=TZ)

    def __call__(self):
        return self.at

    def set(self, at):
        self.at = datetime.datetime.fromisoformat(at).replace(tzinfo=TZ)


def grant(**over):
    g = {"name": "wayfare_posting", "app": APP, "words": ["Post"], "until": "2026-11-21", "perDay": 3,
         "draftsOnly": False}
    g.update(over)
    return {k: v for k, v in g.items() if v is not None}


def write_policy(path, *items, **top):
    obj = {"version": 2, "confirm": ["Send", "Post", "Delete", "Remove", "Buy", "Publish", "Reply"], "block": [],
           "grants": list(items)}
    obj.update(top)
    path.write_text(json.dumps(obj), encoding="utf-8")
    # a new modification time for each write, as the book reads the file again by it
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    return path


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    audit = AuditLog(tmp_path / "audit", now=lambda: clock().astimezone(datetime.timezone.utc))
    path = tmp_path / "policy.json"

    def make(*items, **top):
        write_policy(path, *items, **top)
        return grants.Book(path, audit, now=clock)
    return make, clock, audit, path


def post(app=APP, name="Post", routes=()):
    return grants.Touch(app, name, tuple(routes), "tap")


# ---- the file (§19.1) ----------------------------------------------------------------------------------------------
REFUSALS = [
    (grant(name=None), "#1", "name is missing."),
    (grant(name="bad name"), "bad name", "name may only use letters"),
    (grant(name="con"), "con", "name may only use letters"),
    (grant(app=None), "wayfare_posting", "app is missing."),
    (grant(words=[]), "wayfare_posting", "words must be a list of words, at least one."),
    (grant(words=["Post", 3]), "wayfare_posting", "words must be a list of words, at least one."),
    (grant(until=None), "wayfare_posting", "until must be a date, like 2026-11-21."),
    (grant(until="2026-02-30"), "wayfare_posting", "until must be a date, like 2026-11-21."),
    (grant(until="2026-12-25"), "wayfare_posting", "until is 41 days away; the most is 30."),
    (grant(route=""), "wayfare_posting", "route must be the name of a saved route."),
    (grant(perDay=0), "wayfare_posting", "perDay must be a whole number, 1 or more."),
    (grant(perDay=True), "wayfare_posting", "perDay must be a whole number, 1 or more."),
    (grant(perHour=1.5), "wayfare_posting", "perHour must be a whole number, 1 or more."),
    (grant(draftsOnly="no"), "wayfare_posting", "draftsOnly must be true or false."),
    (grant(quiet={"from": "22:00"}), "wayfare_posting", "quiet needs from and to as HH:MM."),
    (grant(quiet={"from": "24:00", "to": "07:00"}), "wayfare_posting", "quiet needs from and to as HH:MM."),
    (grant(quiet={"from": "07:00", "to": "07:00"}), "wayfare_posting", "quiet's from and to must differ."),
    (grant(revoked=True), "wayfare_posting", "revoked must be a date, like 2026-11-14."),
    (grant(words=["Pay"]), "wayfare_posting", 'payments and purchases are never grantable ("Pay").'),
    (grant(words=["Buy now"]), "wayfare_posting", 'payments and purchases are never grantable ("Buy").'),
    (grant(words=["Delete account"]), "wayfare_posting", 'deleting an account is never grantable ("Delete account").'),
    ("not an object", "#1", "it isn't an object."),
]


@pytest.mark.parametrize("item, name, reason", REFUSALS)
def test_every_refusal_reason_refuses_that_grant_alone(setup, item, name, reason):
    make, *_ = setup
    book = make(item, grant(name="other", words=["Send"]))
    loaded = book.load()
    assert [g.name for g in loaded.grants] == ["other"]  # the others load
    assert len(loaded.refused) == 1
    assert loaded.refused[0].name == name and loaded.refused[0].reason.startswith(reason)


def test_the_refusal_note_is_the_spec_text(setup):
    make, *_ = setup
    book = make(grant(name="shop_pay", words=["Pay"]), grant(name="insta", until="2026-12-25"))
    assert book.notes() == ['Note: grant "shop_pay" is refused: payments and purchases are never grantable ("Pay").',
                            'Note: grant "insta" is refused: until is 41 days away; the most is 30.']


def test_a_blocked_word_is_never_grantable(setup):
    make, *_ = setup
    book = make(grant(words=["Erase"]), block=["Erase"])
    assert book.load().refused[0].reason == 'a blocked word is never grantable ("Erase").'
    book = make(grant(words=["Wipe"]), apps={APP: {"block": ["Wipe"]}})  # the app's own block list
    assert book.load().refused[0].reason == 'a blocked word is never grantable ("Wipe").'


def test_names_equal_ignoring_ascii_case_are_one_name_active_or_revoked(setup):
    make, *_ = setup
    book = make(grant(name="Posting", revoked="2026-11-13"), grant(name="posting"), grant(name="POSTING", words=["Send"]))
    loaded = book.load()
    assert [g.name for g in loaded.grants] == ["Posting"]
    assert [r.name for r in loaded.refused] == ["posting", "POSTING"]
    assert all("already used by an earlier grant" in r.reason for r in loaded.refused)
    assert loaded.grants[0].folder == "posting"


def test_defaults_and_unknown_keys(setup):
    make, *_ = setup
    book = make({"name": "g", "app": APP, "words": ["Send"], "until": "2026-11-20", "colour": "blue"},
                {"name": "h", "app": APP, "words": ["Remove"], "until": "2026-11-20"})
    g, h = book.load().grants
    assert g.per_day == 1 and g.per_hour is None and g.drafts_only is True  # Send reaches others
    assert h.drafts_only is False


def test_a_version_1_file_has_no_grants_and_loads_as_before(tmp_path):
    path = tmp_path / "p.json"
    path.write_text('{"version": 1, "confirm": ["Post"]}', encoding="utf-8")
    rules = policy.load(path)
    assert rules.file is None and rules.confirm == ("Post",)
    with pytest.raises(ValueError, match="grants need version 2"):
        policy.parse({"version": 1, "grants": []})


def test_a_version_2_file_keeps_its_path_for_the_daemon(tmp_path):
    path = write_policy(tmp_path / "p.json", grant())
    rules = policy.load(path)
    assert rules.file == str(path.resolve())
    sent = policy.loads(rules.dumps(), header=True)
    assert sent.file == rules.file and sent.confirm == rules.confirm
    with pytest.raises(ValueError, match="unknown key 'file'"):  # a file can't point at another file
        policy.loads(rules.dumps())


def test_the_built_in_policy_has_no_grants():
    assert policy.DEFAULT.file is None and "file" not in policy.DEFAULT.dumps()


# ---- what a grant covers (§19.2, §19.3) -------------------------------------------------------------------------------
def test_a_covered_touch_notes_the_grant_and_its_count(setup):
    make, *_ = setup
    book = make(grant())
    d = book.decide(post(), "use", 'tap "Post"')
    assert d.kind == "covered" and d.line == 'Note: allowed by grant "wayfare_posting" (1 of 3 today).'
    d = book.decide(post(name="Post update"), "use", 'tap "Post update"')  # whole word, ignoring case
    assert d.line == 'Note: allowed by grant "wayfare_posting" (2 of 3 today).'


def test_app_word_and_route_mismatches_are_not_noted(setup):
    make, *_ = setup
    book = make(grant(route="post_update"))
    for touch in (post(app="com.other"), post(name="Posted"), post(name="Send"), post(routes=["other"]), post()):
        d = book.decide(touch, "use", "")
        assert d.kind == "ask" and d.notes == []
    assert book.decide(post(routes=["post_update"]), "use", "").kind == "covered"


@pytest.mark.parametrize("word", grants.PAYMENT_WORDS + grants.ACCOUNT_WORDS)
def test_no_never_grantable_word_is_ever_covered(setup, word):
    make, *_ = setup
    # even where a grant's own word is in the element's name ("Remove" in a payment or an account deletion)
    book = make(grant(words=["Remove", "Delete", "Post", "Now", "Order", "Account", "Out"], perDay=50))
    for name in (word, f"Remove {word}", f"Post {word}", f"{word} now"):
        assert book.decide(post(name=name), "use", "").kind == "ask"
        assert book.decide(post(name=name), "peek", "").kind == "ask"


@pytest.mark.parametrize("app", grants.NEVER_APPS)
def test_settings_wallet_and_passwords_are_never_covered(setup, app):
    make, *_ = setup
    book = make(grant(app=app, words=["Remove"]))
    assert book.load().grants  # the grant loads (it names no never-grantable word) ...
    assert book.decide(grants.Touch(app, "Remove", (), "tap"), "use", "").kind == "ask"  # ... but covers nothing there


def test_the_doesnt_cover_notes_are_the_spec_text(setup):
    make, clock, *_ = setup
    book = make(grant(name="instagram_posting", perDay=3))
    for _ in range(3):
        assert book.decide(post(), "use", "").kind == "covered"
    d = book.decide(post(), "use", "")
    assert d.kind == "ask" and d.asked == ["limit reached"]
    assert d.notes == ['Note: grant "instagram_posting" doesn\'t cover this (3 of 3 today); asking a person.']

    book = make(grant(name="instagram_posting", perDay=5, perHour=1))
    clock.set("2026-11-14T11:00:00")
    assert book.decide(post(), "use", "").kind == "covered"
    assert book.decide(post(), "use", "").notes == [
        'Note: grant "instagram_posting" doesn\'t cover this (1 of 1 this hour); asking a person.']

    book = make(grant(name="instagram_posting", quiet={"from": "22:00", "to": "07:00"}))
    clock.set("2026-11-14T23:30:00")
    d = book.decide(post(), "use", "")
    assert d.notes == ['Note: grant "instagram_posting" doesn\'t cover this (quiet hours 22:00 to 07:00); asking a '
                       'person.'] and d.asked == ["quiet hours"]
    clock.set("2026-11-15T06:59:00")
    assert book.decide(post(), "use", "").asked == ["quiet hours"]  # a window that crosses midnight
    clock.set("2026-11-15T07:00:00")
    assert book.decide(post(), "use", "").kind == "covered"

    book = make(grant(name="instagram_posting", until="2026-11-21"))
    clock.set("2026-11-21T23:59:00")
    assert book.decide(post(), "use", "").kind == "covered"  # up to the end of that local day
    clock.set("2026-11-22T00:00:00")
    d = book.decide(post(), "use", "")
    assert d.notes == ['Note: grant "instagram_posting" doesn\'t cover this (it ended on 2026-11-21); asking a person.']
    assert d.asked == ["ended"]

    clock.set("2026-11-14T10:00:00")
    book = make(grant(name="instagram_posting", revoked="2026-11-14"))
    d = book.decide(post(), "use", "")
    assert d.notes == ['Note: grant "instagram_posting" doesn\'t cover this (it was revoked on 2026-11-14); asking a '
                       'person.'] and d.asked == ["revoked"]


def test_a_dry_run_counts_nothing(setup, tmp_path):
    make, *_ = setup
    book = make(grant())
    for _ in range(3):
        d = book.decide(post(), "peek", "")
        assert d.kind == "covered" and d.dry == 'It is allowed by grant "wayfare_posting" (1 of 3 today).'
    assert not (tmp_path / "policy.json.uses").exists() or not any((tmp_path / "policy.json.uses").rglob("*/*/*"))
    assert not (tmp_path / "audit").exists()


def test_drafts_only_stops_before_the_touch_and_counts_nothing(setup, tmp_path):
    make, *_ = setup
    book = make(grant(draftsOnly=None))  # absent: true for Post
    d = book.decide(post(), "use", "")
    assert d.kind == "draft" and d.name == "wayfare_posting"
    assert not list((tmp_path / "policy.json.uses").rglob("*/*/*")) if (tmp_path / "policy.json.uses").exists() else True


def test_the_policy_file_is_read_again_at_every_touch(setup):
    make, *_ = setup
    book = make(grant())
    assert book.decide(post(), "use", "").kind == "covered"
    make(grant(revoked="2026-11-14"))  # by hand, or by the other tool
    assert book.decide(post(), "use", "").asked == ["revoked"]


def test_an_unreadable_policy_file_covers_nothing(setup, tmp_path):
    make, *_ = setup
    book = make(grant())
    (tmp_path / "policy.json").write_text("{not json", encoding="utf-8")
    assert book.decide(post(), "use", "").kind == "ask"
    assert "no grant covers anything" in book.notes()[-1]


# ---- the audit line (§23) ------------------------------------------------------------------------------------------
def test_every_use_writes_its_audit_line(setup, tmp_path):
    make, *_ = setup
    book = make(grant())
    book.decide(post(), "use", 'tap "Post"')
    lines = (tmp_path / "audit" / "2026-11.jsonl").read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    assert entry == {"t": "2026-11-14T07:00:00.000Z", "tool": "tapwright", "kind": "grant", "app": APP,
                     "name": "wayfare_posting", "detail": 'tap "Post" (1 of 3 today)'}


def test_a_failed_audit_write_means_not_covered_and_no_use(setup, tmp_path):
    make, clock, audit, path = setup
    (tmp_path / "audit").write_text("a file where the folder should be", encoding="utf-8")
    book = make(grant())
    assert book.decide(post(), "use", "").kind == "ask"
    assert book.counts(book.load().grants[0], clock()) == (0, 0)  # the slot was given back
    assert grants.Book(path, None, now=clock).decide(post(), "use", "").kind == "ask"  # no audit log at all


def test_the_audit_detail_is_redacted(tmp_path):
    audit = AuditLog(tmp_path, scrub=lambda text: text.replace("hunter2", "<home_password>"))
    assert audit.write("grant", name="g", app=APP, detail='tap "Send hunter2"')
    assert "hunter2" not in (tmp_path / os.listdir(tmp_path)[0]).read_text(encoding="utf-8")
    assert not AuditLog(tmp_path, scrub=lambda text: 1 / 0).write("grant", name="g")  # a scrub that fails: no line


def test_the_audit_log_keeps_90_days(tmp_path):
    now = datetime.datetime(2026, 11, 14, tzinfo=datetime.timezone.utc)
    (tmp_path / "2026-08.jsonl").write_text(
        json.dumps({"t": "2026-08-01T00:00:00.000Z", "kind": "grant"}) + "\n"
        + json.dumps({"t": "2026-08-20T00:00:00.000Z", "kind": "grant"}) + "\n", encoding="utf-8")
    (tmp_path / "2026-07.jsonl").write_text(json.dumps({"t": "2026-07-01T00:00:00.000Z"}) + "\n", encoding="utf-8")
    AuditLog(tmp_path, now=lambda: now).prune()
    assert not (tmp_path / "2026-07.jsonl").exists()
    assert "2026-08-20" in (tmp_path / "2026-08.jsonl").read_text(encoding="utf-8")
    assert "2026-08-01" not in (tmp_path / "2026-08.jsonl").read_text(encoding="utf-8")


# ---- use slots (§19.4) -----------------------------------------------------------------------------------------------
def test_slots_are_files_next_to_the_policy_file(setup, tmp_path):
    make, *_ = setup
    book = make(grant(name="Wayfare_Posting", perHour=2))
    d = book.decide(post(), "use", "")
    assert d.use.slots == ["wayfare_posting/2026-11-14T10/h1", "wayfare_posting/2026-11-14/1"]
    day = tmp_path / "policy.json.uses" / "wayfare_posting" / "2026-11-14" / "1"
    body = json.loads(day.read_text(encoding="utf-8"))
    assert body["t"] == "2026-11-14T10:00:00+03:00" and body["tool"] == "tapwright" and body["owner"] == d.use.owner


def test_a_freed_slot_is_reused_and_the_count_is_the_files_present(setup, tmp_path):
    make, *_ = setup
    book = make(grant(perDay=3))
    first = book.decide(post(), "use", "").use
    second = book.decide(post(), "use", "").use
    book.release(first)  # a use proven not delivered gives its slot back
    third = book.decide(post(), "use", "")
    assert third.use.slots == ["wayfare_posting/2026-11-14/1"] and third.counts == "2 of 3"
    assert second.slots == ["wayfare_posting/2026-11-14/2"]


def test_give_back_checks_the_owner(setup, tmp_path):
    make, *_ = setup
    book = make(grant())
    use = book.decide(post(), "use", "").use
    stranger = grants.Use(use.name, "someone-else", list(use.slots), 1, 3)
    book.release(stranger)
    assert (tmp_path / "policy.json.uses" / "wayfare_posting" / "2026-11-14" / "1").exists()


def test_garbage_and_unreadable_slot_files_count_as_uses(setup, tmp_path):
    make, *_ = setup
    book = make(grant(perDay=2))
    day = tmp_path / "policy.json.uses" / "wayfare_posting" / "2026-11-14"
    day.mkdir(parents=True)
    (day / "junk").write_bytes(b"\x00\xff")
    (day / "1").write_text("", encoding="utf-8")
    d = book.decide(post(), "use", "")
    assert d.kind == "ask" and d.notes[0].endswith("(2 of 2 today); asking a person.")


def test_a_failed_day_claim_gives_the_hour_slot_back(setup, tmp_path):
    make, *_ = setup
    book = make(grant(perDay=1, perHour=3))
    day = tmp_path / "policy.json.uses" / "wayfare_posting" / "2026-11-14"

    def another_claimer_first(slot):  # takes the day's only slot while this claim is between the hour and the day
        if slot.name == "h1":
            day.mkdir(parents=True)
            (day / "1").write_text("{}", encoding="utf-8")
    book.hooks["before_create"] = another_claimer_first
    d = book.decide(post(), "use", "")
    assert d.kind == "ask" and d.notes[0].endswith("(1 of 1 today); asking a person.")
    assert os.listdir(tmp_path / "policy.json.uses" / "wayfare_posting" / "2026-11-14T10") == []


def test_limits_hold_across_restarts(setup, tmp_path):
    make, clock, audit, path = setup
    make(grant(perDay=2))
    for _ in range(2):
        assert grants.Book(path, audit, now=clock).decide(post(), "use", "").kind == "covered"  # a new process each time
    assert grants.Book(path, audit, now=clock).decide(post(), "use", "").kind == "ask"
    clock.set("2026-11-15T09:00:00")  # a new local day
    assert grants.Book(path, audit, now=clock).decide(post(), "use", "").kind == "covered"


def test_a_claim_that_crosses_midnight_is_claimed_again_in_the_new_day(setup, tmp_path):
    make, clock, audit, path = setup
    times = iter(["2026-11-14T23:59:59", "2026-11-15T00:00:01"])

    def moving():
        try:
            clock.set(next(times))
        except StopIteration:
            pass
        return clock.at
    book = grants.Book(write_policy(path, grant(perDay=1)), audit, now=moving)
    d = book.decide(post(), "use", "")
    assert d.kind == "covered" and d.use.slots == ["wayfare_posting/2026-11-15/1"]
    assert not (tmp_path / "policy.json.uses" / "wayfare_posting" / "2026-11-14" / "1").exists()


def test_the_check_before_the_touch_moves_a_use_to_the_new_hour(setup, tmp_path):
    make, clock, *_ = setup
    book = make(grant(perDay=5, perHour=1))
    clock.set("2026-11-14T10:59:59")
    touch = post()
    d = book.decide(touch, "use", "")
    clock.set("2026-11-14T11:00:00")
    assert book.still_covers(touch, d.use)
    assert d.use.slots == ["wayfare_posting/2026-11-14T11/h1", "wayfare_posting/2026-11-14/1"]
    assert os.listdir(tmp_path / "policy.json.uses" / "wayfare_posting" / "2026-11-14T10") == []


def test_the_check_before_the_touch_sees_a_revocation(setup):
    make, *_ = setup
    book = make(grant())
    touch = post()
    d = book.decide(touch, "use", "")
    make(grant(revoked="2026-11-14"))
    assert not book.still_covers(touch, d.use)
    assert book.counts(book.load().grants[0], book.now()) == (0, 0)  # given back


def test_a_revocation_between_claim_and_line_wins(setup):
    make, clock, audit, path = setup
    write_policy(path, grant())
    book = grants.Book(path, audit, now=clock, hooks={"after_claim": lambda use: write_policy(
        path, grant(revoked="2026-11-14"))})
    assert book.decide(post(), "use", "").kind == "ask"


def test_pruning_clears_ended_and_revoked_grants(setup, tmp_path):
    make, clock, audit, path = setup
    book = make(grant(name="old", until="2026-11-15"), grant(name="live", words=["Remove"]))
    book.decide(post(), "use", "")
    book.decide(post(name="Remove"), "use", "")
    clock.set("2026-11-16T10:00:00")
    assert book.prune() == ["old"]
    uses = tmp_path / "policy.json.uses"
    assert not (uses / "old").exists() and not (uses / "live" / "2026-11-14").exists() and (uses / "live").exists()


def _claimer(path, audit_dir, tries, out):
    clock = Clock()
    book = grants.Book(path, AuditLog(audit_dir, now=lambda: clock().astimezone(datetime.timezone.utc)), now=clock)
    covered = sum(book.decide(post(), "use", "").kind == "covered" for _ in range(tries))
    out.put(covered)


def test_counts_hold_across_two_processes(setup, tmp_path):
    make, *_ = setup
    make(grant(perDay=7, perHour=None))
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    procs = [ctx.Process(target=_claimer, args=(tmp_path / "policy.json", tmp_path / "audit", 6, out)) for _ in range(2)]
    for p in procs:
        p.start()
    got = [out.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(30)
    assert sum(got) == 7
    assert sorted(os.listdir(tmp_path / "policy.json.uses" / "wayfare_posting" / "2026-11-14")) == \
        sorted(str(k) for k in range(1, 8))
    audit = (tmp_path / "audit" / "2026-11.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(audit) == 7


# ---- the edit lock and the kill switch (§19.5) ------------------------------------------------------------------------
def test_revoke_all_marks_every_grant_writes_lines_and_clears_uses(setup, tmp_path):
    make, clock, audit, path = setup
    book = make(grant(), grant(name="earlier", words=["Remove"], revoked="2026-11-10"), grant(name="b", words=["Send"]))
    book.decide(post(), "use", "")
    assert book.revoke_all() == (["wayfare_posting", "b"], ["earlier"])
    items = json.loads(path.read_text(encoding="utf-8"))["grants"]
    assert [g.get("revoked") for g in items] == ["2026-11-14", "2026-11-10", "2026-11-14"]
    assert not (tmp_path / "policy.json.uses" / "wayfare_posting").exists()
    lines = [json.loads(line) for line in (tmp_path / "audit" / "2026-11.jsonl").read_text("utf-8").splitlines()]
    assert [(e["kind"], e["name"], e["detail"]) for e in lines if e["kind"] == "revoke"] == [
        ("revoke", "wayfare_posting", "grants --revoke-all"), ("revoke", "b", "grants --revoke-all")]
    assert book.decide(post(), "use", "").asked == ["revoked"]


def test_a_stale_writer_cant_undo_a_revoke(setup):
    """A change (a grant created through the dialog, later) and --revoke-all at once: the change holds the edit lock,
    the revoke waits for it, and each applies to the file as it is under the lock, so neither undoes the other."""
    make, clock, audit, path = setup
    make(grant())
    first, second = grants.Book(path, audit, now=clock), grants.Book(path, audit, now=clock)
    revoked: list = []
    inside = threading.Event()

    def during_write():
        inside.set()
        thread.start()
        time.sleep(0.3)  # the revoke is waiting on the lock now
        assert revoked == []

    first.hooks["before_write"] = during_write
    thread = threading.Thread(target=lambda: revoked.append(second.revoke_all()))

    def add(obj):
        obj["grants"].append(grant(name="added", words=["Remove"]))
        return True
    assert first.edit(add) is None
    thread.join(10)
    assert revoked == [(["wayfare_posting", "added"], [])]
    items = json.loads(path.read_text(encoding="utf-8"))["grants"]
    assert [(g["name"], g.get("revoked")) for g in items] == [("wayfare_posting", "2026-11-14"),
                                                              ("added", "2026-11-14")]


def test_a_live_lock_holder_is_never_displaced_and_nothing_is_written(setup, tmp_path):
    make, clock, audit, path = setup
    book = make(grant())
    before = path.read_text(encoding="utf-8")
    token = pidlock.take(tmp_path / "policy.json.edit.lock")  # held by this live process
    assert token is not None
    book.lock_wait = 0.2
    # the file isn't written under a live holder; the kill switch still works, by its `.revoked` mark (review 1.7 A-M5)
    assert book.revoke_all() == (["wayfare_posting"], [])
    assert path.read_text(encoding="utf-8") == before and book.revoked_mark.exists()
    assert book.active() == frozenset()
    pidlock.release(tmp_path / "policy.json.edit.lock", token)
    assert book.revoke_all() == ([], ["wayfare_posting"])  # (the next locked edit folds the mark into the file)
    assert not book.revoked_mark.exists() and '"revoked"' in path.read_text(encoding="utf-8")


def test_pause_doesnt_revoke(setup):
    make, *_ = setup
    book = make(grant())
    active = book.active()
    assert book.revoked_since(active) == []  # nothing changed in the file: a pause leaves the grants standing
    book.revoke_all()
    assert book.revoked_since(active) == ["wayfare_posting"]
