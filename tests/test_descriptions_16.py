"""The 1.6 texts (TOOL-TEXT.md section 4, a draft not yet agreed with Tapline): every new tool and parameter is
described, in the words the tool uses, and the budget for the tool list holds."""
import asyncio
import re

from tapwright import mcp_server
from tapwright.descriptions import EXTRA, INSTRUCTIONS, SHARED, describe


def tools() -> dict:
    return {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}


def params(name: str) -> set[str]:
    """The argument names a tool takes, as an agent writes them (an alias, like `with`, is the written name)."""
    return set(tools()[name].input_schema.get("properties", {}))


def test_drive_and_map_describe_every_parameter():
    new = {"drive": {"to", "route", "with", "account", "then", "by", "dry_run", "brief"},
           "map": {"to", "name", "link", "place", "reflex", "forget_reflex", "forget"}}
    for name, wanted in new.items():
        assert wanted <= params(name), name
        missing = [p for p in sorted(wanted) if f"`{p}" not in describe(name)]  # `dry_run: true`, `with`, ...
        assert not missing, (name, missing)
    assert {"look", "screenshot"} <= params("drive")  # the two every action has: described once, with the actions


def test_run_and_record_describe_what_1_6_added():
    run = describe("run")
    for word in ("`if`", "`optional: true`", "`gone: true`", "`wait_for`", "`brief: true`"):
        assert word in run, word
    record = describe("record")
    for word in ("`route:", "`type_text`", "`as:", "`${variable}`", "`with`", "secure field"):
        assert word in record, word
    assert "route" in params("record") and "as" in params("type_text")


def test_brief_is_a_parameter_of_every_action_and_the_instructions_say_what_it_does():
    for name in ("tap", "long_press", "type_text", "scroll", "toggle", "back", "swipe", "open_url", "wait", "wait_for",
                 "open_app", "alert", "press_button", "rotate", "run", "drive"):
        assert "brief" in params(name), name
    assert "`brief: true` on an action, `run` or `drive` cuts the answer to its first line" in INSTRUCTIONS
    assert "at most 12 lines" in INSTRUCTIONS and "`…<n> more: look shows them.`" in INSTRUCTIONS  # (the spec's wording)
    assert 'to: "target"` for a bar over it' in INSTRUCTIONS  # 1.13.1 (3): off_screen is no longer a "scroll to target" case
    assert "`off_screen` means no scroll can reach it" in INSTRUCTIONS
    assert "the tap was delivered, and nothing on screen changed within <N> s" in INSTRUCTIONS  # §3's note, 1.13.1 (8)
    assert "under the threshold still matches but is named" in describe("compare_screen")  # 1.13.1 (2)
    assert "`off_route`" in INSTRUCTIONS


def test_the_agreed_texts_around_the_1_6_ones_are_unchanged():
    assert SHARED["run"].startswith("Runs several steps in one call, in order, and answers once:")
    assert "Use it to fill a form or walk a flow you're sure of; read the answer before the next batch. A step may carry" \
        in SHARED["run"]
    assert SHARED["record"].startswith("Record a run as a script that replays without an AI:")
    assert INSTRUCTIONS.index("`Settled in 0.31 s.`") < INSTRUCTIONS.index("`brief: true`") < INSTRUCTIONS.index("`Note:` lines")
    assert INSTRUCTIONS.index("`expect_failed`") < INSTRUCTIONS.index("`off_route`") < INSTRUCTIONS.index("`internal`")


def test_no_text_claims_what_session_report_does_not_say():
    text = describe("session_report")
    for word in ("`Time:`", "thinking time", "new process"):
        assert word in text, word
    assert "map knows" not in text and "faster model" not in text  # not built: the report doesn't say these


def test_the_new_texts_name_no_brand_of_model():
    for name in ("drive", "map", "run", "record", "session_report"):
        assert not re.search(r"haiku|sonnet|opus|claude|gpt|codex", describe(name), re.I), name


# 1.5's text (commit 63d7d99): the instructions and every tool's description, in tokens of 4 characters.
BASE_TOKENS = 4632
# PLAN-1.6 Part D item 7 sets the growth at 600 tokens, new tools included. Measured 2026-11-12: 641. The surplus is
# the `off_route` and `alert` lines the workers added before this pass. Tapline's review of the 1.6 texts then added
# what brief never cuts and drive's journey rule (about 80 more, 720 in all): the owner's bar is speed and reliability,
# and an agent that knows what is never cut reads less again. The bound leaves room for rounding only.
#
# 1.13.1 (W35, 2026-11-13): +190 tokens (912 measured, 920 allowed). TOOL-TEXT lagged the agreed clarifications, and they
# are now written in: the `not_actionable` row says `off_screen` means no scroll can reach the element (about 60, since the
# four touches scroll for themselves), the §3 no-change note (about 45), and the `compare_screen` match that names a
# change under the threshold (about 70). Each is a thing an agent would otherwise get wrong and pay a call for.
# 1.7 (W134): + the `notifications` tool (AGENT-TOOLS §24.2), about 40 tokens: a new tool's paragraph.
ALLOWED_GROWTH = 1432  # 1.8 (W138): + the `replay` repair sentence (AGENT-TOOLS §26), about 70 tokens (1362 before it); 1.8 (W137): + the `explore` tool (AGENT-TOOLS §25), 187 tokens (1175 before it); # + the test-account line (66) and the host dialog sentence (36), both agreed 2026-11-13; + the two lines about text from others and platform terms (about 90 tokens, AGENT-TOOLS 1.16 §21.2)


def test_the_tool_list_stays_within_its_budget():
    """The whole tool list grows by at most about 600 tokens over 1.5's (four characters a token), and the driver's
    tool list stays at 3,000 or less (tests/test_driver.py)."""
    names = set(SHARED) | set(EXTRA)
    now = (len(INSTRUCTIONS) + sum(len(describe(n)) for n in names)) / 4
    assert now - BASE_TOKENS <= ALLOWED_GROWTH, now - BASE_TOKENS
