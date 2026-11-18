"""Private button content may be absent from XCTest's app tree: recover only by hitting the button itself.

The report's geometry is synthetic test data, not a recording of the reporting app's accessibility hierarchy.
"""
import dataclasses

import pytest

from test_daemon import finish, make, run, started
from test_review_17_c1 import APP
from test_review_17_c4_touches import real_dispatch
from test_runner_v5 import node
from tapwright import daemon as daemon_module, policy, ui
from tapwright.control import Refused


SHAPES = [
    ("Settings", (370, 98, 44, 45), "Other", "", (381, 109, 22, 23)),
    ("Account", (28, 594, 374, 49), "StaticText", "Account", (76, 607, 296, 24)),
    ("Sign out", (28, 246, 374, 45), "StaticText", "Sign out", (186, 256, 58, 24)),
    ("Continue", (16, 733, 398, 57), "StaticText", "Continue", (179, 748, 72, 27)),
]
NO_CONFIRM = policy.parse({"version": 1, "confirm": [], "block": []})


def setup(shape=SHAPES[1], *, placement="omitted", kind="Button", enabled=True):
    label, rect, content_type, content_label, content_rect = shape
    content = node(content_type, content_rect, n=content_label, l=content_label)
    control = node(kind, rect, [content] if placement == "inside" else (), n="control", l=label, d=not enabled)
    siblings = [control, content] if placement == "sibling" else [control]
    source = ui.xml_of_tree(node("Application", (0, 0, 430, 932), [
        node("Window", (0, 0, 430, 932), siblings)], n="Example", b=APP))
    d = make()
    s = d.session
    s.screen = (430, 932)
    s.tree = ui.read(source, s.screen).items
    s.last_source = source
    target = next(e for e in s.tree if e.name == "control")
    inside = ui.Hit(content_type, content_label, content_label, content_rect, APP)
    direct = ui.Hit(kind, "control", label, rect, APP)

    async def hit_at(x, y):
        s.hit_asks.append((x, y))
        return s.center_answer if (x, y) == target.center else s.background_answer

    s.hit_at, s.center_answer, s.background_answer = hit_at, inside, direct
    return d, target, inside, direct
