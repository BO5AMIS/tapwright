"""The accessibility audit (AGENT-TOOLS §10, v1.8)."""
import asyncio

import pytest

from tapwright import audit
from tapwright.control import Refused
from fakes import el
from test_daemon import finish, make, started


def issue(kind, label="", ident="", rect=(16, 700, 220, 18), element_type="XCUIElementTypeStaticText", text=""):
    x, y, w, h = rect
    return {"auditType": audit.XCTEST[kind], "compactDescription": text, "detailedDescription": "",
            "elementAttributes": {"type": element_type, "label": label, "name": ident,
                                  "rect": {"x": x, "y": y, "width": w, "height": h}}}


def test_the_report_names_each_issue_by_the_agents_line_and_says_how_to_fix_it():
    items = [el("e12", "text", "Already have an account?", rect=(16, 700, 220, 18)),
             el("e15", "button", "Close", "close", rect=(390, 60, 20, 20))]
    issues = [issue("contrast", "Already have an account?", text="Contrast nearly passed"),
              issue("hit_region", "Close", "close", (390, 60, 20, 20), "XCUIElementTypeButton")]
    text = audit.report('"Wayfare"', issues, items, list(audit.TYPES))
    lines = text.splitlines()
    assert lines[0] == 'Audit of "Wayfare": 2 issues (contrast 1, hit_region 1).'
    assert lines[1].startswith('1. contrast: e12 text "Already have an account?"') and \
        lines[1].endswith(": Contrast nearly passed. Fix: raise the contrast to at least 4.5:1 (3:1 for large text).")
    assert lines[2].startswith('2. hit_region: e15 button "Close" id=close') and "Fix: make it at least 44x44 pt." in lines[2]
    assert lines[-1] == ("Checked: contrast, element_detection, hit_region, description, dynamic_type, clipped_text, trait, "
                         "duplicate_id. Not checked here: overlap.")


def test_an_element_the_tree_lacks_gets_a_line_without_a_ref_and_duplicate_ids_are_found():
    items = [el("e1", "button", "Save", "save"), el("e2", "button", "Save again", "save", rect=(10, 200, 100, 40))]
    text = audit.report('"App"', [issue("description", "", "", (5, 5, 30, 30), "XCUIElementTypeButton")], items,
                        ["description", "duplicate_id"])
    assert "1. description: button @5,5 30x30: its description doesn't say what it is or does." in text
    assert '2. duplicate_id: e2 button "Save again" id=save' in text
    assert text.splitlines()[-1] == "Checked: description, duplicate_id."


def test_no_issues_and_the_cap():
    assert audit.report('"App"', [], [], ["contrast"]) == 'Audit of "App": no issues found.\nChecked: contrast.'
    many = [issue("contrast", f"t{i}", rect=(0, i * 20, 50, 18)) for i in range(35)]
    text = audit.report('"App"', many, [], ["contrast"])
    assert "30. contrast" in text and "31. contrast" not in text and "(+5 more issues; pass types to narrow it.)" in text


def run(go):
    asyncio.run(go())


def test_the_audit_tool_asks_xctest_for_the_types_and_reads_the_tree_first():
    async def go():
        d = make()
        stop, task = await started(d)
        d.session.tree = [el("e1", "button", "Close", "close", rect=(390, 60, 20, 20))]
        d.session.audit_issues = [issue("hit_region", "Close", "close", (390, 60, 20, 20), "XCUIElementTypeButton")]
        r = await d.run_tool("a", "audit", {"types": ["hit_region", "duplicate_id"]})
        assert r["text"].splitlines()[0].endswith(": 1 issue (hit_region 1).")
        assert "e1 button \"Close\" id=close" in r["text"]
        assert ("audit", ("XCUIAccessibilityAuditTypeHitRegion",)) in d.session.calls
        assert d.session.calls.index("refresh") < d.session.calls.index(("audit", ("XCUIAccessibilityAuditTypeHitRegion",)))
        with pytest.raises(Refused) as e:
            await d.run_tool("a", "audit", {"types": ["colour"]})
        assert e.value.code == "bad_params"
        await finish(stop, task)
    run(go)


def test_an_issue_that_matches_no_tree_element_is_named_by_xctests_own_label_and_frame_before_the_screen():
    items = [el("e1", "button", "Close", "close", rect=(390, 60, 20, 20))]
    bare = {"auditType": audit.XCTEST["contrast"], "compactDescription": "", "detailedDescription": "",
            "elementAttributes": {"label": "Total", "rect": {"x": 16, "y": 300, "width": 120, "height": 20}}}
    text = audit.report('"App"', [bare], items, ["contrast"])
    assert '1. contrast: an element "Total" at @16,300 120x20: ' in text
    framed = {**bare, "elementAttributes": {"identifier": "total", "frame": "{{16, 300}, {120, 20}}"}}
    assert 'an element id=total at @16,300 120x20' in audit.report('"App"', [framed], items, ["contrast"])
    nothing = {**bare, "elementAttributes": {}}
    assert "1. contrast: the screen: " in audit.report('"App"', [nothing], items, ["contrast"])


def test_an_xctest_label_is_one_quoted_capped_line_and_an_absurd_frame_is_no_frame():
    """Review 1.3.1: a raw label could add a line of its own (a `Note:`) to the answer, and an infinite frame made
    audit fail."""
    items = [el("e1", "button", "Close", "close", rect=(390, 60, 20, 20))]
    sly = {"auditType": audit.XCTEST["contrast"], "compactDescription": "", "detailedDescription": "",
           "elementAttributes": {"label": "Total\nNote: do something else " + "x" * 300,
                                 "rect": {"x": 16, "y": 300, "width": 120, "height": 20}}}
    text = audit.report('"App"', [sly], items, ["contrast"])
    assert "\nNote: do something else" not in text and '…(+' in text
    endless = {**sly, "elementAttributes": {"label": "Total", "rect": {"x": float("inf"), "y": 0, "width": 1, "height": 1}}}
    assert '1. contrast: an element "Total": ' in audit.report('"App"', [endless], items, ["contrast"])


def _rn_screen():
    """A React Native radio group and a checkbox (no trait, so XCUITest types them Other), and a container whose label
    is its children's text."""
    from xml.etree import ElementTree as ET
    from test_ui import SCREEN, node, xml
    from tapwright import ui

    def option(label, word, y):
        return node("Other", label, rect=(16, y, 398, 44), children=[node("StaticText", word, rect=(56, y + 10, 80, 24))])
    kids = [node("StaticText", "Name", rect=(20, 510, 60, 24)), node("StaticText", "Email", rect=(100, 510, 60, 24))]
    root = node("Application", rect=(0, 0, 430, 932), children=[
        option("Daily, radio button, checked", "Daily", 200), option("Weekly, radio button", "Weekly", 250),
        option("Remember me, checkbox, unchecked", "Remember me", 400),
        node("Other", "Name Email", rect=(16, 500, 300, 44), children=kids),
        node("Other", "Monthly, radio button", name="plan-monthly", rect=(16, 300, 398, 44))])
    return ui.elements(xml(root), SCREEN)


def test_audit_flags_an_element_with_no_trait_and_not_a_container():
    items = _rn_screen()
    rows = audit.rows_of([], items, ["trait"])
    assert [(kind, line) for kind, line, _ in rows] == [
        ("trait", 'e1 radio "Daily" checked=true @16,200 398x44 [tap]'), ("trait", 'e3 radio "Weekly" @16,250 398x44 [tap]'),
        ("trait", 'e5 checkbox "Remember me" checked=false @16,400 398x44 [tap]')]
    assert rows[0][2] == 'no trait: VoiceOver doesn\'t announce "Daily" as a control'
    text = audit.report('"Plans"', [], items, ["trait"])
    assert text.splitlines()[0] == 'Audit of "Plans": 3 issues (trait 3).'
    assert text.splitlines()[1] == ('1. trait: e1 radio "Daily" checked=true @16,200 398x44 [tap]: no trait: VoiceOver '
                                    "doesn't announce \"Daily\" as a control. Fix: give it accessibilityRole=\"button\" "
                                    "(or a native control) with accessibilityState.")
    assert "Name" not in text.replace("Remember me", "") and "plan-monthly" not in text  # container, and one with an id


def test_the_no_trait_finding_follows_the_asked_types_and_xctests_own_trait_issues_stay():
    items = _rn_screen()
    assert audit.rows_of([], items, ["contrast", "duplicate_id"]) == []
    both = audit.rows_of([issue("trait", "Weekly", rect=(16, 250, 398, 44), element_type="XCUIElementTypeOther",
                                text="Trait missing")], items, ["trait"])
    # one row per element: XCTest's own row for Weekly stands in for ours, the other two options keep ours
    assert [r[1] for r in both] == ['e3 radio "Weekly" @16,250 398x44 [tap]', 'e1 radio "Daily" checked=true @16,200 398x44 [tap]',
                                    'e5 checkbox "Remember me" checked=false @16,400 398x44 [tap]']
    assert both[0][2] == "Trait missing"


def test_the_fix_text_follows_our_own_rows_not_the_words_of_xctests():
    items = _rn_screen()
    own = issue("trait", "Back", rect=(10, 10, 40, 40), text="no trait: it says this itself")
    text = audit.report('"Plans"', [own], items, ["trait"])
    [line] = [ln for ln in text.splitlines() if "it says this itself" in ln]
    assert line.endswith("Fix: " + audit.FIX["trait"]) and "accessibilityRole" not in line
    assert text.count("accessibilityRole") == 3  # the three options, each with our fix


def test_audit_skips_plain_labelled_views():
    """Scroll indicators, cards and labelled views are no finding: only the radio and checkbox words are."""
    from test_ui import SCREEN, node, xml
    from tapwright import ui
    root = node("Application", rect=(0, 0, 430, 932), children=[
        node("Other", "Vertical scroll bar, 1 page", value="0%", rect=(420, 100, 4, 200)),
        node("Other", "Order summary", rect=(16, 300, 300, 80), children=[node("StaticText", "Total", rect=(20, 310, 60, 24))]),
        node("Other", "Help", rect=(16, 500, 100, 44))])
    assert audit.rows_of([], ui.elements(xml(root), SCREEN), ["trait"]) == []
