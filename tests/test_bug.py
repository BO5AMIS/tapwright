"""AGENT-TOOLS 1.16.2 §29.1, §29.5, §29.9: the signature with the shared vectors (both tools must agree), and the page."""
from tapwright import bug

CRASH_GOT = "the app crashed: TypeError: undefined is not an object (evaluating 'trip.days.map')"


def test_the_crash_of_27_1_is_7bc2e91c152d416e():
    sig = bug.signature("crash", {"route": ["trips", "[id]"], "tab": "trips"}, CRASH_GOT,
                        "TypeError: undefined is not an object", ["*TripDetail.tsx:48 renderDays", "x"])
    assert sig == "7bc2e91c152d416e"


def test_the_observed_note_on_a_signature_place_is_29146f71e709878d():
    note = "Total shows $12.50 after 2 coupons at 09:21:07"
    assert bug.normalize_got(note) == "Total shows $# after # coupons at #"
    assert bug.signature("observed", {"signature": "c41f9a07d2", "parts": {"ids": ["a"]}}, note) == "29146f71e709878d"


def test_the_place_key_json():
    assert bug.place_key_json({"route": ["trips", "[id]"], "tab": "trips"}) == '{"route":["trips","[id]"],"tab":"trips"}'
    assert bug.place_key_json({"tab": "t", "route": ["a"], "layer": "sheet"}) == '{"layer":"sheet","route":["a"],"tab":"t"}'
    assert bug.place_key_json({"signature": "c41f9a07d2", "parts": {"x": 1}}) == '{"signature":"c41f9a07d2"}'
    assert bug.place_key_json(None) == "null"


def test_the_normalizing_vectors():
    assert bug.normalize_got("Total shows $12.50 after 2 coupons") == "Total shows $# after # coupons"
    assert bug.normalize_got("Order 7b9e3c1a-0d52-4f10-9a77-1c2b3d4e5f60 failed at 2026-11-14T09:21:07Z") == "Order # failed at #"
    assert bug.normalize_got("trip-42 not found (0x7ffee3a1c8d0)") == "trip-# not found (#)"
    assert bug.normalize_got("  many   spaces\nsecond line 9  ") == "many spaces"
    assert bug.normalize_got("id deadbeef and facade12 and 5f3a9c01b2") == "id deadbeef and # and #"


def test_the_frame_symbol_vectors():
    assert bug.frame_symbol("*TripDetail.tsx:48 renderDays") == "renderDays"
    assert bug.frame_symbol("3 Wayfare 0x0000000102a4b3c8 -[TripViewController renderDays] + 148") == \
        "-[TripViewController renderDays]"
    assert bug.frame_symbol("*TripDetail.tsx:48") == "unsymbolicated"
    assert bug.frame_symbol("*bundle.js:1:2 <anonymous>") == "unsymbolicated"
    assert bug.frame_symbol("4 Wayfare 0x0000000102a4b3c8 + 148") == "unsymbolicated"
    assert bug.top_app_symbol(["RN.js:1 f", "*TripDetail.tsx:48 renderDays"]) == "renderDays"
    assert bug.top_app_symbol(["RN.js:1 f"]) == "unsymbolicated"
    assert bug.top_app_symbol([]) == "unsymbolicated"


def test_tapwrights_own_native_frames():
    # diagnostics.describe_crash writes `<n> <* or space> <image> <symbol> +<offset>`
    frames = [" 0   libsystem_kernel.dylib __pthread_kill +8", " 3 * Wayfare TripViewController.renderDays(_:) +148"]
    assert bug.top_app_symbol(frames) == "TripViewController.renderDays(_:)"
    assert bug.top_app_symbol([" 2 * Wayfare +1234"]) == "unsymbolicated"
    assert bug.frame_shown(" 3 * Wayfare TripViewController.renderDays(_:) +148") == "*Wayfare TripViewController.renderDays(_:)"
    assert bug.frame_shown(" 0   libsystem_kernel.dylib 0x1a2b3c __pthread_kill + 8") == "libsystem_kernel.dylib __pthread_kill"


def test_the_exception_type():
    assert bug.exception_type("TypeError: undefined is not an object") == "TypeError"
    assert bug.exception_type("EXC_BAD_ACCESS (SIGSEGV)") == "EXC_BAD_ACCESS"


def test_what_keeps_the_signature_and_what_makes_another():
    key = {"route": ["trips", "[id]"], "tab": "trips"}
    sig = bug.signature("crash", key, CRASH_GOT, "TypeError: x", ["*TripDetail.tsx:48 renderDays"])
    assert bug.signature("crash", key, CRASH_GOT, "TypeError: y", ["*TripDetail.tsx:51 renderDays"]) == sig
    assert bug.signature("crash", key, CRASH_GOT, "RangeError: x", ["*TripDetail.tsx:48 renderDays"]) != sig
    assert bug.signature("crash", key, CRASH_GOT, "TypeError: x", ["*TripDetail.tsx:48 renderOther"]) != sig
    native = bug.signature("crash", {"signature": "ab"}, "the app crashed: EXC_BAD_ACCESS", "EXC_BAD_ACCESS (SIGSEGV)",
                           [" 3 * Wayfare 0x0000000102a4b3c8 -[TripViewController renderDays] + 148"])
    assert bug.signature("crash", {"signature": "ab"}, "the app crashed: EXC_BAD_ACCESS", "EXC_BAD_ACCESS (SIGSEGV)",
                         [" 3 * Wayfare 0x00000001aaaaaaaa -[TripViewController renderDays] + 12"]) == native
    assert bug.signature("slow", {"signature": "a"}, "3.5 s to settle") == bug.signature("slow", {"signature": "a"}, "4.1 s to settle")
    # a place's name is not part of it: only the key is
    assert bug.signature("dead_end", {"signature": "a"}, "x") != bug.signature("dead_end", {"signature": "b"}, "x")


def test_the_title_cuts_a_long_got_after_redaction():
    got = "A" * 50 + "B" * 50
    t = bug.title("js_error", None, got)
    assert t == f"JS error on an unnamed screen: {'A' * 45} … {'B' * 30}"
    assert bug.title("crash", "place_3", "short") == "Crash on place_3: short"


def test_steps_written_for_a_person():
    assert bug.step_line({"tool": "tap", "target": {"id": "trips-tab"}}, "Trips") == 'Tap "Trips" (id trips-tab)'
    assert bug.step_line({"tool": "tap", "target": {"id": "trip-row-1"}}) == "Tap id trip-row-1"
    assert bug.step_line({"row": True, "index": 1}) == "Tap the 1st row of the list."
    assert bug.step_line({"tool": "type_text", "target": {"label": "Email"}, "text": "${typed}"}) == 'Type something in "Email"'
    assert bug.step_line({"tool": "scroll"}) == "Scroll down"
    assert bug.step_line({"tool": "scroll", "to": "end"}) == "Scroll to the end"
    assert bug.step_line({"tool": "swipe", "x0": 10, "y0": 500, "x1": 12, "y1": 100}) == "Swipe up"
    assert bug.step_line({"tool": "swipe", "x0": 300, "y0": 500, "x1": 10, "y1": 490}) == "Swipe left"
    assert bug.step_line({"tool": "back"}) == "Go back"
    assert bug.step_line({"tool": "open_url", "url": "wayfare://trips/42?token=abc"}) == "Open the link wayfare://trips/[value]"
    assert bug.step_line({"tool": "tap", "target": {"label": "Delete"}}, needs_person=True) == 'Tap "Delete" (needs a person)'
    assert bug.step_line({"tool": "press_button", "name": "home"}) == "Run press_button"
    assert bug.step_line({"tool": "tap", "target": {"label": "Hi"}}, others=True) == 'Tap "Hi" (from others)'


def test_paths_are_cut_to_their_file_names():
    assert bug.strip_paths(r"at C:\Users\Jane Doe\proj\TripDetail.swift:48 renderDays") == "at TripDetail.swift:48 renderDays"
    assert bug.strip_paths("/Users/jane/Library/x/Wayfare.app/Wayfare crashed") == "Wayfare crashed"
    assert bug.strip_paths("loaded /private/var/containers/Bundle/App.app/Frameworks/hermes") == "loaded hermes"


def _meta(**over):
    meta = {"id": "bug_7bc2e91c152d416e", "signature": "7bc2e91c152d416e", "place_key": {"signature": "c41f9a07d2"},
            "source": {"how": "explore", "run": "20261114T091500Z", "finding": "f3"},
            "seen": {"first": "2026-11-12T10:02:44.120Z", "last": "2026-11-14T09:21:07.512Z", "count": 2,
                     "builds": ["1.5.0 (219)", "1.5.0 (220)"], "sources": []},
            "reproduced": {"checked": False, "why": "no phone connected"}, "picture_included": False}
    meta.update(over)
    return meta


def _finding(**over):
    f = {"id": "f3", "kind": "crash", "app": "com.example.wayfare", "app_version": "1.5.0", "build": "220",
         "tool": "tapwright", "tool_version": "1.8.5", "when": "2026-11-14T09:21:07.512Z",
         "device": {"model": "iPhone16,1", "os": "iOS 19.1"}, "place": "place_4", "path_from": "home",
         "path": [{"tool": "tap", "target": {"id": "trips-tab"}}, {"tool": "tap", "target": {"id": "trip-row-1"}}],
         "expected": "The app keeps running.", "got": "the app crashed: EXC_BAD_ACCESS",
         "picture": {"file": "findings/f3.jpg", "private": True}, "logs": ["09:21:07.440 error boom"],
         "crash": {"reason": "EXC_BAD_ACCESS (SIGSEGV)", "frames": [" 3 * Wayfare 0x1 renderDays +148"]}, "count": 1}
    f.update(over)
    return f


def test_the_page_has_its_nine_sections_in_order():
    page = bug.markdown(_finding(system="share sheet"), _meta(names=["Trips", None]), None)
    heads = [line for line in page.splitlines() if line.startswith("## ")]
    assert heads == ["## What happened", "## How to see it", "## Reproduced", "## Environment", "## Where", "## Logs",
                     "## Picture", "## Seen before"]
    assert page.startswith("# Crash on place_4: the app crashed: EXC_BAD_ACCESS\n")
    assert "Start on home.\n1. Tap \"Trips\" (id trips-tab)\n2. Tap id trip-row-1" in page
    assert "not checked (no phone connected)" in page
    assert "- Place: place_4 (signature c41f9a07d2)" in page
    assert "- Frames: *Wayfare renderDays" in page
    assert "- A system sheet was in front: share sheet." in page
    assert 'A picture exists and is private. To include it: bug_report {"bug": "bug_7bc2e91c152d416e", "with_picture": true}' in page
    assert "2 times; first 2026-11-12, last 2026-11-14; builds 1.5.0 (219), 1.5.0 (220)." in page


def test_a_worst_case_page_fits_90_lines():
    steps = [{"tool": "tap", "target": {"id": f"b{i}"}} for i in range(19)]
    f = _finding(path=steps, logs=[f"line {i} " + "x" * 400 for i in range(30)],
                 crash={"reason": "EXC", "frames": [f" {i} * App sym{i} +1" for i in range(12)]})
    page = bug.markdown(f, _meta(), None)
    assert len(page.splitlines()) <= 90
    assert "The first 4 steps are in repro.json." in page
    assert all(len(line) <= 300 for line in page.splitlines() if line.startswith("line "))


def test_replace_section_and_html():
    page = bug.markdown(_finding(), _meta(), None)
    again = bug.replace_section(page, "Seen before", ["3 times."])
    assert again.endswith("## Seen before\n3 times.\n")
    assert "## Picture" in again
    out = bug.page_html(bug.replace_section(page, "What happened", ["<script>alert(1)</script>"]))
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "http" not in out.replace('<html lang="en">', "")


def test_app_folders():
    assert bug.app_folder("com.example.wayfare") == "com.example.wayfare"
    other = bug.app_folder("com.example.Wayfare")
    assert other.startswith("_com.example.wayfare-") and len(other.split("-")[-1]) == 16
    assert bug.app_folder("com.example.Wayfare") != bug.app_folder("com.example.WAYFARE")
    assert bug.app_folder("../x").startswith("_x-")
