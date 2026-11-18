"""An explicit runner identity failure must not turn into an unchecked ordinary tap."""
import asyncio
import json
import types

import pytest

from tapwright.actionlog import ActionLog
from tapwright.control import Refused
from tapwright.device import ConnectionLost, DeviceError
from tapwright.session import HIT_IDENTITY_REFUSED, Session
from test_171_button_content import setup
from test_daemon import finish, run, started
from test_review_17_c4_touches import real_dispatch
from test_runner_v5 import Runner, ready


def test_runner_identity_failure_is_terminal_and_contains_no_private_error_text():
    private = "UNRELATED-SAVED-IDENTITY@example.invalid"
    runner = Runner(hits=DeviceError(f"WebDriverAgent: unsupported operation: {HIT_IDENTITY_REFUSED} {private}"))
    session = ready(runner)
    with pytest.raises(Refused) as error:
        asyncio.run(session.hit_at(215, 792))
    assert error.value.code == "not_actionable" and private not in str(error.value)
    assert error.value.hit_check == {"gate": "identity_unproven", "phase": "runner", "probes": 0}
    assert not any(method == "POST" and path.endswith("/actions") for method, path, _ in runner.requests)
