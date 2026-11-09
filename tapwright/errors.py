"""The failures an agent can see. Every one is a `Refused(code, message)` (control.py), whose
code is one of CODES; `ToolError` is the same class under the name docs/AGENT-TOOLS.md §4 uses."""
from .control import Refused

ToolError = Refused

CODES = ("not_found", "ambiguous", "not_actionable", "stale", "busy", "paused", "locked", "reconnecting",
         "not_connected", "timeout", "bad_params", "unsupported", "internal")
