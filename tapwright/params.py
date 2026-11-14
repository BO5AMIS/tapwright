"""The argument names each tool takes. The daemon answers `bad_params` for any other name, so an argument that does
nothing (`audit` given `labels`) is said, not silently ignored. tests/test_params.py holds this table to the MCP
tools' signatures (tapwright/mcp_server.py), so the two can't drift."""
from __future__ import annotations

from .control import Refused

_SHOT = ("look", "screenshot")
_BRIEF = ("brief",)  # AGENT-TOOLS 1.13 §13.4: on every action and on run; never on look
_AIM = ("target", "index")  # `index` beside `target` is moved inside it by the daemon

ALLOWED: dict[str, tuple[str, ...]] = {
    "look": ("screenshot", "width", "labels", "ocr"),
    "tap": (*_AIM, "force", *_SHOT, "dry_run", *_BRIEF),
    "long_press": (*_AIM, "seconds", "force", *_SHOT, "dry_run", *_BRIEF),
    "type_text": ("text", *_AIM, "replace", "submit", "force", "as", *_SHOT, "dry_run", *_BRIEF),  # (as: a recording's variable)
    "scroll": ("direction", "amount", "to", *_AIM, "force", *_SHOT, "dry_run", *_BRIEF),  # (force: the daemon refuses it with no_action; the MCP tool omits it)
    "toggle": (*_AIM, "value", "force", *_SHOT, "dry_run", *_BRIEF),
    "back": (*_SHOT, "dry_run", *_BRIEF),
    "open_url": ("url", *_SHOT, *_BRIEF),
    "wait": ("seconds", *_SHOT, *_BRIEF),
    "wait_for": ("text", "id", "label", "gone", "timeout", "network_idle", *_SHOT, *_BRIEF),
    "run": ("steps", "trace", "dry_run", *_SHOT, *_BRIEF),
    "drive": ("to", "route", "with", "account", "then", "by", "dry_run", *_SHOT, *_BRIEF),
    "record": ("start", "stop", "name", "path", "route", "action"),
    "replay": ("name", "path", *_SHOT, "update_references", "repair"),  # (update_references: the CLI's, not the MCP tool's)
    "audit": ("types",),
    "ux_check": (),
    "media": ("add", "remove", "confirm"),
    "logs": ("since", "level", "grep", "limit"),
    "crash": ("all", "pull"),
    "performance": ("launch", "seconds", "tap"),
    "filmstrip": ("frames", "interval_ms", "frame_width", "tap"),
    "compare_screen": ("name", "update", "width", "threshold", "ignore"),
    "apps": (),
    "notifications": ("open",),
    "session_report": ("since",),
    "map": ("to", "name", "link", "place", "reflex", "forget_reflex", "forget"),
    "open_app": ("app", "language", "region", "text_size", *_SHOT, *_BRIEF),
    "network": ("mode",),
    "close_app": ("app",),
    "alert": ("action", *_SHOT, *_BRIEF),
    "press_button": ("name", *_SHOT, *_BRIEF),
    "swipe": ("x0", "y0", "x1", "y1", "seconds", *_SHOT, "dry_run", *_BRIEF),
    "rotate": ("to", *_SHOT, *_BRIEF),
    "journey": ("start", "goal", "persona", "conditions", "allow", "eyes", "stop", "reached", "why", "export", "stress",
                "mode", "target", "survives", "done", "finding"),
    "explore": ("minutes", "steps", "depth", "from", "focus", "fill", "continue", "account", "guide"),
}
# Names the daemon takes that the MCP tools' signatures don't have (tests/test_params.py holds the table to this)
NOT_IN_MCP: dict[str, set[str]] = {t: {"index"} for t, names in ALLOWED.items() if "index" in names}
NOT_IN_MCP["replay"] = {"update_references"}
NOT_IN_MCP["scroll"] = NOT_IN_MCP["scroll"] | {"force"}
NOT_IN_MCP["wait_for"] = NOT_IN_MCP.get("wait_for", set()) | {"network_idle"}  # Tapline's: answered unsupported, not bad_params


def unknown(tool: str, args: dict) -> list[str]:
    """The names in `args` that `tool` doesn't take; none for a tool the table doesn't know (the daemon refuses it)."""
    allowed = ALLOWED.get(tool)
    return [] if allowed is None else [k for k in args if k not in allowed]


def refusal(tool: str, names: list[str], prefix: str = "") -> str:
    takes = ", ".join(ALLOWED[tool]) or "no arguments"
    return f"{prefix}{tool} doesn't take {', '.join(names)}; it takes {takes}."


def check(tool: str, args: dict, prefix: str = "") -> None:
    """`bad_params` when `args` has a name `tool` doesn't take."""
    names = unknown(tool, args)
    if names:
        raise Refused("bad_params", refusal(tool, names, prefix))
