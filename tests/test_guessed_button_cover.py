"""A navbar's oversized sibling frame is only a cover guess, never touch authorization.

All geometry and runner responses here are synthetic. Real Session dispatch remains in use.
"""
import dataclasses
import xml.etree.ElementTree as ET

import pytest

from test_171_button_content import NO_CONFIRM, setup
from test_daemon import finish, run, started
from test_review_17_c1 import APP
from test_review_17_c4_touches import real_dispatch
from test_runner_v5 import node
from tapwright import daemon as daemon_module, ui
from tapwright.control import Refused


def navbar(*, label="Cancel", top=82, nested=False):
    rect = (16.4, top, 48, 49)
    content_rect = (32.4, top + 15, 16, 19)
    d, _, private, direct = setup((label, rect, "Other", "", content_rect))
    control = node("Button", rect, [node("Button", content_rect, n="post", l="Post")] if nested else (),
                   n="control", l=label)
    address = node("Button", (25.6, top, 378, 49), n="URL", l="Address")
    nav = node("NavigationBar", (0, top - 3, 430, 55), [control, address], n="Browser")
    source = ui.xml_of_tree(node("Application", (0, 0, 430, 932), [
        node("Window", (0, 0, 430, 932), [nav])], n="Example", b=APP))
    s = d.session
    s.last_source, s.tree = source, ui.read(source, s.screen).items
    target = next(e for e in s.tree if e.name == "control")
    address_hit = ui.Hit("Button", "URL", "Address", tuple(address["r"]), APP)
    return d, target, private, direct, address_hit


def changed_source(source, change):
    root = ET.fromstring(source)
    control = next(n for n in root.iter() if n.get("name") == "control")
    if change == "disabled":
        control.set("enabled", "false")
    elif change == "changed":
        control.set("label", "Delete account")
    elif change == "alert":
        window = next(n for n in root.iter() if n.get("type") == "XCUIElementTypeWindow")
        ET.SubElement(window, "XCUIElementTypeAlert", type="XCUIElementTypeAlert",
                      x="50", y="500", width="300", height="100")
    return ET.tostring(root, encoding="unicode")


def weak_hit(kind, direct, private):
    return {
        "unanswered": None,
        "holder": ui.Hit("Window", "", "", (0, 0, 430, 932), APP),
        "foreign": dataclasses.replace(direct, bundle="com.example.foreign"),
        "changed": dataclasses.replace(direct, label="Delete account"),
        "other_button": dataclasses.replace(direct, name="post", label="Post"),
        "private": private,
    }[kind]
