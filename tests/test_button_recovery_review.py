"""Review regressions: refreshed ancestry and fresh hard bars cannot authorize recovery."""
import asyncio
import dataclasses
import xml.etree.ElementTree as ET

import pytest

from tapwright import ui
from tapwright.control import Refused
from test_171_button_content import SHAPES, setup
from test_daemon import finish, run, started
from test_dialog import asked
from test_guessed_button_cover import changed_source, navbar
from test_review_17_c4_touches import real_dispatch
