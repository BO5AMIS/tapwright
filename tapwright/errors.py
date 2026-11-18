"""The failures an agent can see. Every one is a `Refused(code, message)` (control.py), whose
code is one of CODES (docs/AGENT-TOOLS.md §4); HTTP_STATUS is the status each one has on the API."""

from .control import Refused

CODES = ("not_found", "ambiguous", "not_actionable", "stale", "busy", "paused", "locked", "not_answering", "reconnecting",
         "not_connected", "timeout", "bad_params", "unsupported", "not_allowed", "expect_failed", "off_route", "internal")

HTTP_STATUS = {"not_found": 404, "ambiguous": 409, "not_actionable": 409, "stale": 409, "busy": 409,
               "paused": 409, "locked": 503, "not_answering": 503, "reconnecting": 503, "not_connected": 503, "timeout": 504,
               "bad_params": 400, "unsupported": 400, "not_allowed": 403, "expect_failed": 409, "off_route": 409, "internal": 500}

ToolError = Refused  # the name agent-facing code reads better with

# A locked phone (1.7.8): the agent may call `unlock`, which types the passcode saved for this phone (serve's own vault,
# phone.passcode; the agent never sees it), else a person unlocks it. The XCTest prompt keeps its own words: a person
# enters the passcode there, never the tool.
PHONE_LOCKED = ("the phone is locked; call unlock (it types the passcode saved for this phone), or ask the user to "
                "unlock it")
