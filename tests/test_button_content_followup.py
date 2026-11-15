"""Synthetic sibling-content regressions; no recorded app hierarchy or phone access.

A sibling remains unrelated. Only a separate live hit on the original button can
authorize an uncovered point, and the real Session path checks it again at POST.
"""
import dataclasses

import pytest

from test_171_button_content import NO_CONFIRM, SHAPES, setup
from test_daemon import finish, run, started
from test_review_17_c1 import APP
from test_review_17_c4_touches import real_dispatch
from test_runner_v5 import node
from tapwright import daemon as daemon_module, ui
from tapwright.control import Refused


def shape(label="Account", content="Account"):
    _, rect, kind, _, content_rect = SHAPES[1]
    return label, rect, kind, content, content_rect
