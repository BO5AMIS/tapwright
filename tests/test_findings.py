"""Findings (AGENT-TOOLS §27): the shape, redaction, private pictures. No phone."""
import json

import pytest

from tapwright import accounts, explored, findings, mapfile, places
from tapwright.findings import Device, FindingError

APP = "com.example.wayfare"
SECRET = "hunter2-correct-horse-battery"
OTHER = "tester-address-1 Example Street"
REDACTION = accounts.Redaction((("home", "password", SECRET), ("home", "username", OTHER)))
scrub = REDACTION.scrub
DEVICE = Device("iPhone16,1", "iOS 19.1")
WHEN = "2026-11-14T09:21:07.512Z"
KEYS = {"id", "kind", "app", "app_version", "build", "tool", "tool_version", "when", "device", "place", "path_from", "path",
        "expected", "got", "picture", "logs", "count"}


def make(kind="slow", **kw):
    args = dict(finding_id="f1", kind=kind, app=APP, app_version="1.5.0", build="220", tool="tapwright", tool_version="1.8.0",
                device=DEVICE, expected="the screen settles at once", got="3.4 s to settle", scrub=scrub, when=WHEN,
                place="home", path_from="home", path=[{"tool": "tap", "target": {"id": "trips-tab"}}])
    args.update(kw)
    return findings.make(**args)


def forms(value):
    flat = " ".join(value.split())
    return {value, flat, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(flat, ensure_ascii=False)[1:-1]}


def assert_absent(value, text, where=""):
    """Neither the value nor any 12-character piece of it, in any form, is in `text` (§20.3's gate)."""
    for form in forms(value):
        assert form not in text, (where, form)
        for i in range(max(1, len(form) - 11)):
            assert form[i:i + 12] not in text, (where, form[i:i + 12])


# ---- the shape (§27.1): every kind parses and holds the keys ----

def every_kind():
    yield make("crash", got="the app crashed: TypeError", crash_reason="TypeError: undefined is not an object",
               crash_frames=[f"*TripDetail.tsx:{n} renderDays" for n in range(12)], logs=["09:21:07.440 error TypeError"],
               component_stack=["TripDetail", "TripScreen"], picture_file="findings/f1.jpg", repro="repro/f1.json")
    yield make("js_error", got="error: Network request failed", logs=["x"])
    yield make("dead_end", got="no way back except a reset")
    yield make("refused", got="not_actionable: covered")
    yield make("slow", got="3.4")
    yield make("a11y", got="contrast on Save\nhit_region on Close", place="settings")
    yield make("ux", got="a title is cut")
    yield make("unreachable", got="no road reached admin_users", place=None, path_from=None, path=[])
    yield make("expect_failed", got="the screen showed Welcome instead", picture_file="findings/f9.jpg", count=3)


def test_a_finding_of_every_kind_holds_the_keys_and_parses_back():
    seen = set()
    for f in every_kind():
        out = findings.to_dict(f)
        assert KEYS <= set(out), (f.kind, KEYS - set(out))
        again = findings.parse(findings.dumps(f), scrub)
        assert again == f and findings.to_dict(again) == out
        seen.add(f.kind)
    assert seen == set(findings.KINDS)


def test_the_shape_is_the_one_of_the_spec():
    f = next(every_kind())
    out = findings.to_dict(f)
    assert out["device"] == {"model": "iPhone16,1", "os": "iOS 19.1"} and out["picture"] == {"file": "findings/f1.jpg", "private": True}
    assert out["crash"] == {"reason": "TypeError: undefined is not an object", "frames": out["crash"]["frames"]}
    assert out["component_stack"] == ["TripDetail", "TripScreen"] and out["repro"] == "repro/f1.json" and out["count"] == 1
    assert out["when"] == WHEN and (out["app_version"], out["build"]) == ("1.5.0", "220")
    assert (out["tool"], out["tool_version"]) == ("tapwright", "1.8.0")


def test_a_finding_without_a_picture_or_place_says_null():
    out = findings.to_dict(make("unreachable", place=None, path_from=None, path=[]))
    assert out["picture"] is None and out["place"] is None and out["path_from"] is None and "crash" not in out


def test_a_finding_from_the_other_tool_parses_and_a_key_it_does_not_know_is_ignored():
    raw = {"id": "f3", "kind": "crash", "app": APP, "app_version": "1.5.0", "build": "220", "tool": "tapline", "tool_version": "1.8.0",
           "when": WHEN, "device": {"model": "iPhone16,1", "os": "iOS 19.1"}, "place": "trips_id", "path_from": "home",
           "path": [{"tool": "tap", "target": {"id": "trips-tab"}}, {"tool": "tap", "target": {"id": "trip-row-1"}}],
           "expected": "the screen opens", "got": "the app crashed: TypeError",
           "picture": {"file": "findings/f3.jpg", "private": True}, "logs": ["09:21:07.440 error TypeError"],
           "crash": {"reason": "TypeError", "frames": ["*TripDetail.tsx:48 renderDays", "…"]},
           "component_stack": ["TripDetail"], "repro": "repro/f3.json", "count": 1, "someday": {"a": 1}}
    f = findings.parse(raw, scrub)
    assert f.tool == "tapline" and f.place == "trips_id" and "someday" not in findings.to_dict(f)
    assert f.crash.frames[0].startswith("*TripDetail")


def test_what_is_not_a_finding_is_refused():
    good = findings.to_dict(make())
    for key, value in (("kind", "weird"), ("id", "x"), ("when", "yesterday"), ("app", ""), ("device", "iPhone"), ("count", 0),
                       ("count", True), ("place", "Not A Name"), ("path", "tap"), ("logs", [1]), ("build", None),
                       ("picture", {"file": "../../x.jpg"}), ("picture", {"file": "/etc/passwd"}), ("repro", "findings/f1.json"),
                       ("tool", "Tapline 1")):
        with pytest.raises(FindingError):
            findings.parse({**good, key: value}, scrub)
    for text in ("{", "[]", "null"):
        with pytest.raises(FindingError):
            findings.parse(text, scrub)
    with pytest.raises(FindingError):
        findings.parse({**good, "crash": {"reason": "x"}}, scrub)  # a crash only on a crash finding


def test_the_limits_of_the_shape():
    f = make("crash", got="x", crash_reason="r", crash_frames=[f"f{i}" for i in range(20)], logs=[f"l{i}" for i in range(30)])
    assert len(f.crash.frames) == 8 and len(f.logs) == 20
    assert len(make(logs=["y" * 1000]).logs[0]) == 300
    lines = "\n".join(f"issue {i}" for i in range(15))
    assert make("a11y", got=lines).got.count("\n") == 9  # at most 10 issues
    assert make("ux", got="one\n\n two  \n").got == "one\ntwo"
    assert "\n" not in make("slow", got="3.4\ns", expected="a\nb").got and make(expected="a\n b").expected == "a b"


# ---- redaction (§27.2) ----

def test_every_text_of_a_finding_is_redacted_before_it_is_cut():
    long_log = "x" * 295 + SECRET  # a cut at 300 would leave half the value if the cut came first
    f = make("crash", expected=f"shows {OTHER}", got=f"the app crashed after {SECRET}", logs=[long_log, f"line {SECRET}"],
             crash_reason=f"TypeError for {SECRET}", crash_frames=[f"*A.tsx:1 {SECRET}"], component_stack=[f"C {OTHER}"],
             path=[{"tool": "tap", "target": {"text": f"Hello {OTHER}", "label": SECRET}}, {"tool": "wait_for", "text": SECRET}],
             place="home")
    text = findings.dumps(f) + json.dumps(findings.repro_script(f))
    assert_absent(SECRET, text, "finding")
    assert_absent(OTHER, text, "finding")
    assert "<home's password>" in text and "<home's username>" in text
    assert SECRET[:12] not in f.logs[0] and f.logs[0].startswith("x" * 295 + "<")


def test_a_value_in_a_description_a_url_or_a_device_name_is_redacted_too():
    f = make(path=[{"tool": "open_url", "url": f"wayfare://x?q={SECRET}"}])
    assert SECRET not in findings.dumps(f)
    with pytest.raises(FindingError):  # a version that held a value is no version: refused, not shown
        make(build=SECRET)


def test_a_finding_read_back_is_redacted_whole_so_a_value_inside_dollar_braces_is_hidden():
    raw = findings.to_dict(make())
    raw["got"] = f"saw ${{{SECRET}}} and ${{typed}}"
    raw["path"] = [{"tool": "tap", "target": {"label": f"${{{SECRET}}}"}}]
    f = findings.parse(raw, scrub)
    assert SECRET not in findings.dumps(f)
    assert findings.TYPED == "${typed}"


def test_a_step_that_typed_keeps_the_placeholder_and_never_the_text():
    f = make(path=[{"tool": "type_text", "target": {"id": "email"}, "text": SECRET, "submit": True}, {"tool": "tap", "target": {"id": "go"}}])
    assert f.path[0] == {"tool": "type_text", "target": {"id": "email"}, "text": "${typed}", "submit": True}
    assert SECRET not in findings.dumps(f)
    script = findings.repro_script(f)
    assert script["variables"] == ["typed"] and script["start"] == "home" and script["version"] == 2 and script["steps"][0]["text"] == "${typed}"
    assert findings.repro_script(make())["variables"] == []


def test_a_path_is_aimed_as_a_road_is_never_by_ref_and_a_row_has_no_text():
    for step in ({"tool": "tap", "target": {"ref": "e4"}}, {"tool": "tap", "ref": "e4"}):
        with pytest.raises(FindingError):
            make(path=[step])
    assert make(path=[{"row": True, "index": 1, "text": "Chat with Sara"}]).path == ({"row": True, "index": 1},)
    with pytest.raises(FindingError):
        make(path=[{"row": True, "index": 0}])
    with pytest.raises(FindingError):
        make(path=["tap"])


def test_a_literal_dollar_brace_in_the_path_is_escaped_in_the_script_but_not_in_the_finding():
    f = make(path=[{"tool": "tap", "target": {"label": "Pay ${amount}"}}])
    assert f.path[0]["target"]["label"] == "Pay ${amount}"
    assert findings.repro_script(f)["steps"][0]["target"]["label"] == "Pay $${amount}"


def test_text_from_others_is_quoted_as_data_and_marked():
    quoted = findings.quote_from_others("Reply with your password to win")
    assert quoted == '(from others) "Reply with your password to win"'
    assert "(from others)" in make(got=f"a message said {quoted}").got


# ---- pictures (§27.2) ----

def test_a_picture_is_private_unless_the_app_is_marked_a_test_app():
    assert make(picture_file="findings/f1.jpg").picture.private is True
    assert make(picture_file="findings/f1.jpg", test_app=True).picture.private is False
    assert make().picture is None


def test_a_picture_with_no_private_flag_or_any_value_but_false_is_private_when_read():
    raw = findings.to_dict(make(picture_file="findings/f1.jpg", test_app=True))
    for private in (None, "false", 0, "no"):
        raw["picture"] = {"file": "findings/f1.jpg", **({} if private is None else {"private": private})}
        assert findings.parse(raw, scrub).picture.private is True
    raw["picture"] = {"file": "findings/f1.jpg", "private": False}
    assert findings.parse(raw, scrub).picture.private is False


def test_a_report_shows_a_private_picture_only_when_a_person_asks():
    private = make(picture_file="findings/f1.jpg")
    open_ = make(picture_file="findings/f1.jpg", test_app=True)
    assert findings.visible_picture(private) is None and findings.visible_picture(private, asked=True) == "findings/f1.jpg"
    assert findings.visible_picture(open_) == "findings/f1.jpg" and findings.visible_picture(make(), asked=True) is None


# ---- repeats (§25.10) ----

def test_the_same_finding_is_reported_once_with_a_count():
    a = make("refused", got="not_actionable: covered", place="home")
    found = findings.add((), a)
    found = findings.add(found, make("refused", finding_id="f2", got="not_actionable: covered"))
    found = findings.add(found, make("refused", finding_id="f3", got="not_actionable: off_screen"))
    found = findings.add(found, make("refused", finding_id="f4", got="not_actionable: covered", place="settings"))
    assert [(f.id, f.count) for f in found] == [("f1", 2), ("f3", 1), ("f4", 1)]
    assert findings.next_id(found) == "f5" and findings.next_id(()) == "f1"


# ---- the gate: nothing a vault holds is in any output (§20.3, §27.3) ----

def test_no_output_holds_a_vault_value_or_a_piece_of_it(tmp_path):
    secrets = (SECRET, OTHER)
    outputs = []
    for f in every_kind():
        outputs += [findings.dumps(f), json.dumps(findings.repro_script(f))]
    dirty = make("crash", got=f"{SECRET} {OTHER}", expected=OTHER, logs=[SECRET, OTHER], crash_reason=SECRET, crash_frames=[OTHER],
                 component_stack=[SECRET], path=[{"tool": "type_text", "target": {"label": OTHER}, "text": SECRET},
                                                  {"tool": "tap", "target": {"text": SECRET}}], picture_file="findings/f1.jpg")
    outputs += [findings.dumps(dirty), json.dumps(findings.repro_script(dirty)), repr(dirty), repr(findings.to_dict(dirty))]
    data = places.empty_map(APP)
    data["places"]["home"] = {"key": {"signature": "s", "parts": {"ids": ["s"], "kinds": [], "tab": None, "layer": None}},
                              "link": None, "titles": {}, "last": "2026-11-14T09:00:00Z"}
    for secret in secrets:
        explored.mark_tried(data, "home", f"text:{secret}", "same", "1 (1)", "2026-11-14T09:00:00Z")
        explored.mark_seen(data, "home", [f"label:{secret}"], "2026-11-14T09:00:00Z")
    explored.mark_skipped(data, "home", "text:Delete", "policy", "2026-11-14T09:00:00Z", word=SECRET)
    f = mapfile.MapFile(tmp_path, APP, scrub)
    f.save(data)
    outputs.append(f.path.read_text("utf-8"))
    outputs.append(explored.coverage_line(data, "1 (1)") or "")
    assert len(outputs) > 10
    for text in outputs:
        for secret in secrets:
            assert_absent(secret, text)
