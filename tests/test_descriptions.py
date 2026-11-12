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
         "apps", "session_report", "open_app", "viewer", "close_app", "alert", "press_button", "swipe", "rotate"]
ONLY_HERE = {"close_app", "alert", "press_button", "rotate", "network"}
OLD = ["list_apps", "launch_app", "terminate_app"]


def test_the_instructions_mention_every_error_code_and_reason():
    assert not [c for c in CODES if f"`{c}`" not in INSTRUCTIONS]
    assert not [r for r in REASONS if r not in INSTRUCTIONS]


def test_the_instructions_explain_targets_answers_and_safety():
    for word in ("target", "ref", "force", "Settled in", "screenshot", "user's own phone", "Note:"):
        assert word in INSTRUCTIONS, word
    assert not any(old in INSTRUCTIONS for old in OLD)


def test_the_instructions_are_the_shared_core_then_what_tapwright_adds():
    assert INSTRUCTIONS.startswith("You can see and drive a real mobile app, to test it. Work in a loop: "
                                   "look, act, read the answer.\n\nWorkflow\n")
    core, _, ours = INSTRUCTIONS.partition("\n\nTapwright drives the whole iPhone")
    assert core.endswith("- Give the live view's link to the user only. It carries a key.")
    assert ours.startswith(" through real touches and reads the accessibility tree.\n- System alerts")
    assert "Tapline" not in INSTRUCTIONS and "check_layout" not in INSTRUCTIONS


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


def test_the_docs_name_only_the_shared_tool_set():
    for path in ("README.md", "AGENTS.md", "docs/setup.md"):
        text = (ROOT / path).read_text(encoding="utf-8")
        for old in OLD:
            assert old not in text, f"{path} still names {old}"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Tools", 1)[1].split("\n## ", 1)[0]
    named = set(re.findall(r"`([a-z_]+)`", section))
    assert set(TOOLS) <= named, sorted(set(TOOLS) - named)
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    testing = agents.split("## Testing an app", 1)[1].split("\n## ", 1)[0]
    for tool in ("apps", "open_app", "look", "tap", "type_text", "scroll", "wait_for", "alert"):
        assert f"`{tool}`" in testing, tool
    for code in ("not_found", "ambiguous", "not_actionable", "stale", "busy", "paused", "locked",
                 "reconnecting", "timeout"):
        assert f"`{code}`" in testing, code
    for reason in REASONS:
        assert reason in testing, reason

