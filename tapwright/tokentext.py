"""The token rules' words (AGENT-TOOLS 1.17 §30), in one place: a wording change Tapline and Tapwright agree on is one
edit here. §30.1's block heads the MCP instructions; §30.2's `Known:` line and §30.3's `Hint:` lines are added to
answers. `--no-hints` (§30.4) turns all of them off: the instructions and answers are then 1.8.5's, word for word.
"""
from __future__ import annotations

# Tapwright's own replay call, filled into `<replay>` (§30.1, §30.2)
REPLAY = 'drive {"route": "<name>"}'

# §30.1, word for word in both tools (each fills <replay>)
RULES = (
    "Working well with this tool (fewer calls, fewer tokens):\n"
    "- Read each answer: it says what changed. Don't look again just to check.\n"
    "- When you know the next steps, send them as one `run`, with an `expect` on the steps that matter.\n"
    "- Target by id when there is one.\n"
    "- Ask for a screenshot only to judge how something looks; the text answer is enough to act.\n"
    "- `Known:` lists screens reachable in one call (" + REPLAY + "). Use it instead of tapping your way there.\n"
    "- On a long or unknown task, hand the driving to one small, fast agent and check its result; never two drivers.\n"
    "- A bug report is `bug_report`, not a file you write.\n"
)

KNOWN_MAX = 8  # §30.2: at most 8 names, then `+N more`


def known_line(names: list[str]) -> str | None:
    """§30.2: `Known: <name>, <name>, ... (one call each: <replay>)`; None when nothing is known."""
    if not names:
        return None
    shown = names[:KNOWN_MAX]
    more = f", +{len(names) - KNOWN_MAX} more" if len(names) > KNOWN_MAX else ""
    return f"Known: {', '.join(shown)}{more} (one call each: {REPLAY})"


# §30.3: one sentence each, naming one call. Keys are what "at most once per session" counts.
HINTS = {
    "form": "Hint: this screen is a form: fill it and save it in one `run`, with an `expect` on the save step.",
    "known_way": "Hint: this way is known: {call} reaches it in one call.",
    "screenshot": "Hint: the text answer already listed what changed; ask for a screenshot only to judge how something looks.",
    "long_task": "Hint: on a long task, hand the driving to one small, fast agent and check its result (`run` for known steps).",
    "logs": "Hint: `logs` shows the app's own log since this error.",
}

LONG_TASK_CALLS = 15  # a session's top-level calls after which the long-task hint may come
KNOWN_WAY_TAPS = 3  # single taps in a row along known roads before the known-way hint


def hint(kind: str, **fill: str) -> str:
    return HINTS[kind].format(**fill) if fill else HINTS[kind]
