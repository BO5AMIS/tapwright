"""The words agents read: the MCP instructions name every error code, tools carry the shared
descriptions, and the docs name only the shared tool set."""
import asyncio
import re
from pathlib import Path

from tapwright import mcp_server
from tapwright.descriptions import EXTRA, INSTRUCTIONS, SHARED, describe

ROOT = Path(__file__).resolve().parent.parent
CODES = ["not_found", "ambiguous", "not_actionable", "stale", "busy", "paused", "locked", "in_background",
         "reconnecting", "not_connected", "timeout", "bad_params", "unsupported", "not_allowed", "internal"]
REASONS = ["disabled", "off_screen", "covered", "under_tab_bar", "behind_alert", "no_action"]
TOOLS = ["network", "audit", "ux_check", "media", "compare_screen", "filmstrip", "performance", "logs", "crash", "run", "record", "replay", "look", "tap", "long_press", "type_text", "scroll", "toggle", "back", "open_url", "wait", "wait_for",
         "apps", "session_report", "map", "drive", "open_app", "viewer", "close_app", "alert", "press_button", "swipe", "rotate", "journey", "notifications", "unlock", "explore", "bug_report"]
ONLY_HERE = {"close_app", "alert", "press_button", "rotate", "network", "notifications", "unlock"}
OLD = ["list_apps", "launch_app", "terminate_app"]


def test_the_instructions_mention_every_error_code_and_reason():
    assert not [c for c in CODES if f"`{c}`" not in INSTRUCTIONS]
    assert not [r for r in REASONS if r not in INSTRUCTIONS]


def test_the_instructions_explain_targets_answers_and_safety():
    for word in ("target", "ref", "force", "Settled in", "screenshot", "user's own phone", "Note:"):
        assert word in INSTRUCTIONS, word
    assert not any(old in INSTRUCTIONS for old in OLD)


def test_instructions_describe_agent_directed_whole_phone_control():
    assert "whole-phone access" in INSTRUCTIONS
    assert "Permission judgment belongs to you" in INSTRUCTIONS
    assert "existing authorization" in INSTRUCTIONS
    assert "No app list or label-based approval" in INSTRUCTIONS


def test_shared_texts_are_pinned_word_for_word():
    """A few exact sentences from the shared tool text, so an edit here can't drift from Tapline's."""
    assert SHARED["tap"].startswith("Tap an element, as a finger would. `target` is one of `ref`, `id`, `label`, "
                                    "`text` (with `index` to pick among several) or `x` and `y`.")
    assert "`force: true` skips the off-screen, covered, under-the-tab-bar and behind-an-alert checks (never disabled)." \
        in SHARED["tap"]
    assert SHARED["wait"] == ("Wait `seconds` (0.1-30, required) for an animation, a load or navigation, then answer "
                              "what changed. Out-of-range values are `bad_params`. Answers `Waited 1.0 s.` and the changes.")
    assert SHARED["viewer"].endswith("Their touches go first, and they can pause you. Give the link to the user only.")
    assert SHARED["back"].endswith("`not_actionable: no_action` when there is nothing to go back to.")
    assert EXTRA["tap"] == ("makes a real touch at the element's centre, or at the centre of its visible part "
                            "when a bar covers the centre.")
    assert "`dry_run: true` on a touch or on `run` says what would happen without doing it." in INSTRUCTIONS
    assert SHARED["swipe"].startswith("Drag from `x0, y0` to `x1, y1`, in points, over `seconds` (default 0.35).")


def test_every_tool_carries_its_description():
    tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    assert set(tools) == set(TOOLS)
    for name, tool in tools.items():
        if name in ONLY_HERE:
            expected = EXTRA[name]
        elif name in EXTRA:
            expected = f"{SHARED[name]}\nTapwright: {EXTRA[name]}"
        else:
            expected = SHARED[name]
        assert len(expected) >= 40 and tool.description == expected == describe(name), name


def test_shared_and_tapwright_only_tools_are_the_documented_ones():
    assert set(SHARED) == set(TOOLS) - ONLY_HERE
    assert ONLY_HERE <= set(EXTRA) and set(EXTRA) <= set(TOOLS)
    assert "wait" not in EXTRA  # no Tapwright line: the shared paragraph only


def test_current_docs_name_real_tools_and_current_control_contract():
    for path in ("README.md", "AGENTS.md", "docs/setup.md"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert not any(old in text for old in OLD)
        assert "1.7.5" in text
    docs = (ROOT / "docs/AGENT-TOOLS.md").read_text(encoding="utf-8")
    for tool in TOOLS:
        assert tool in docs
    assert "Permission judgment" in docs
