"""The failures an agent can see. Every one is a `Refused(code, message)` (control.py), whose
code is one of CODES (docs/AGENT-TOOLS.md §4); HTTP_STATUS is the status each one has on the API."""

from .control import Refused

CODES = ("not_found", "ambiguous", "not_actionable", "stale", "busy", "paused", "locked", "reconnecting",
         "not_connected", "timeout", "bad_params", "unsupported", "internal")

HTTP_STATUS = {"not_found": 404, "ambiguous": 409, "not_actionable": 409, "stale": 409, "busy": 409,
               "paused": 409, "locked": 503, "reconnecting": 503, "not_connected": 503, "timeout": 504,
               "bad_params": 400, "unsupported": 400, "internal": 500}

ToolError = Refused  # the name agent-facing code reads better with
