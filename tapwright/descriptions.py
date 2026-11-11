"""The words agents read, from docs/AGENT-TOOLS.md section 7 and the shared tool text. SHARED holds each
tool paragraph Tapline and Tapwright carry word for word; EXTRA is what only Tapwright adds (for the
tools Tapwright alone has, the whole text). INSTRUCTIONS is the MCP `instructions`: the shared core, a
blank line, then what Tapwright adds."""

_CORE = (
    'You can see and drive a real mobile app, to test it. Work in a loop: look, act, read the answer.\n'
    '\n'
    'Workflow\n'
    '- `apps` lists what you can drive. `open_app` picks one. `look` reads the screen.\n'
    "- Every action answers with what changed. Read it before the next step: check that the action did what you meant, not only that it didn't fail.\n"
    "- When the screen is mid-change, `wait` or `wait_for` instead of acting on it. Don't use fixed waits when `wait_for` fits.\n"
    '\n'
    'Targets\n'
    '- Tools that act on an element take one `target` with exactly one of: `ref` (from your last list, like "e3"), `id` (exact identifier, React Native testID), `label` (exact, ignoring case), `text` (visible text: exact first, then partial), or `x` and `y` (points, for things with no element).\n'
    '- `index` (0-based, in reading order) picks among several matches of `id`, `label` or `text`.\n'
    '- Prefer `id`, then `ref`, then `label`, then `text`. Use `x` and `y` last.\n'
    '\n'
    'Answers\n'
    '- The first line says what was done, like `Tapped e3 button "Log in" id=login-button @24,610 354x48.`\n'
    '- Then `Same screen. Changes:` with `+` new, `-` gone, `~` changed, `↕` moved elements (a `~` line marked `off screen` is a change out of view, and has no ref); or `No visible change.`; or the full list when the screen changed or most of it did. Elements still on screen keep their refs, and anything new counts on from the highest ref given. An element that went away and comes back may have its old ref or a new one: read it from the answer. Only `look` starts again at e1.\n'
    '- `Alert:`, `Keyboard:` and similar header lines come first when they changed.\n'
    '- `Settled in 0.31 s.` is how long the screen took to settle. `wait` answers `Waited 1.0 s.` instead.\n'
    '- `Note:` lines come last. Read them.\n'
    '- Positions and sizes are in points, origin top-left.\n'
    '\n'
    'Errors: `Error <code>: <message>`. The message says what to do next.\n'
    '- `not_found`: nothing matches. `look`, then pick another target.\n'
    "- `ambiguous`: several match. Retry with one candidate's ref, or add `index`.\n"
    '- `not_actionable`: the first word is the reason: `disabled`, `off_screen`, `covered`, `under_tab_bar`, `behind_alert`, `no_action`. Fix the cause (`scroll` with `to: "target"`, close the alert) or pass `force: true`. For `no_action`, pick another element or tool.\n'
    '- `stale`: a person changed the screen since your list. `look` again.\n'
    '- `busy`: a person is using the app. Wait a few seconds, retry.\n'
    '- `paused`: the user paused you. Ask them to resume.\n'
    '- `locked`: the phone is locked. Ask the user to unlock it.\n'
    '- `in_background`: the app is in the background. Ask the user to open it.\n'
    '- `not_answering`: the app in front stopped answering. Close it and open it again (`close_app`, then `open_app`, where the tool has them), or ask the user to.\n'
    '- `reconnecting`: the link dropped. Wait a few seconds, then `look`.\n'
    '- `not_connected`: nothing to drive. Follow the setup steps in the message.\n'
    '- `timeout`: no answer in time. The action may have happened: `look` before retrying.\n'
    "- `bad_params`: the call's arguments are invalid. Fix the call.\n"
    "- `unsupported`: this tool can't do it. Use the alternative the message names.\n"
    "- `not_allowed`: outside the apps you were given. The first word is the reason: `outside_apps` (another app is in front, or the call names one: go back with `open_app`) or `no_apps` (you were given none: tell the user).\n"
    '- `internal`: a bug. Report it.\n'
    '\n'
    'Screenshots\n'
    "- Actions send none unless you pass `screenshot: true`. `look` sends one: `screenshot: false` skips it, `width` (160-1320 px, default 600) sizes it, `labels: true` draws each actionable element's box and ref.\n"
    '- Screenshots cost image tokens. Drive by text, and `look` at the picture only when you need it.\n'
    '\n'
    'Safety\n'
    '- A person may be watching or driving in the live view (`viewer`). Their input goes first.\n'
    "- Don't buy, subscribe, send messages, post, delete data, or change account, privacy or security settings, unless the user asked for exactly that step. Ask when unsure.\n"
    "- Use test accounts and test data the user gives you. Don't sign in with credentials you find.\n"
    "- Don't read or repeat private content you see beyond what the test needs.\n"
    "- Give the live view's link to the user only. It carries a key."
)

_TAPWRIGHT = (
    'Tapwright drives the whole iPhone through real touches and reads the accessibility tree.\n'
    '- System alerts (permissions, notifications) show in the header as `Alert:`. Answer them with `alert`.\n'
    '- `press_button` presses Home or volume. `rotate` turns the phone. `swipe` drags between two points. `close_app` quits an app, and `open_app` launches it.\n'
    "- The header shows `Keyboard: shown` or `hidden`. Its keys aren't listed: type with `type_text`.\n"
    '- Stay in the app under test: if a link takes you elsewhere, go back with `open_app`.\n'
    "- On the owner's own phone you can read and touch only the apps you were given. Anything else answers `not_allowed`, and nothing of another app is shown.\n"
    "- A touch on a button or switch with a policy word (Buy, Delete, Send, Sign out and the like) needs a person's OK in the live view: `not_allowed: needs_confirmation` means a person is being asked, so make the same call again once they allow it, or ask your user. `not_allowed: blocked` is final: don't try another way. `force` doesn't skip it.\n"
    "- `dry_run: true` on `tap`, `long_press`, `toggle`, `type_text`, `swipe`, `scroll` and `back` checks the call and says what it would do, touching nothing.\n"
    "- It's the user's own phone. The safety rules above matter most here."
)

INSTRUCTIONS = _CORE + "\n\n" + _TAPWRIGHT

SHARED = {
    'look': (
        'Read the current screen: one line per element (ref, role, text, id, position and size in points,'
        ' actions), plus a screenshot. `screenshot` (default true), `width` (160-1320 px, default 600) '
        "and `labels` (draw each actionable element's box and ref) shape the picture. Answers the full "
        'list, starting with a header. Long text is cut; `wait_for` with the text checks all of it.'
    ),
    'tap': (
        'Tap an element, as a finger would. `target` is one of `ref`, `id`, `label`, `text` (with `index`'
        ' to pick among several) or `x` and `y`. `force: true` skips the off-screen, covered, '
        'under-the-tab-bar and behind-an-alert checks (never disabled). `look: false` answers only the '
        'first line. `screenshot: true` adds a picture. Answers what was tapped and what changed.'
    ),
    'long_press': (
        'Press and hold an element (context menus, drag handles). `target` is as for `tap`; `seconds` is '
        'how long to hold (default 1.0). Answers what changed.'
    ),
    'type_text': (
        'Type `text` into an input. `target` picks the input; without it, the focused input, else the '
        'only one, else `ambiguous`; with no input at all, `not_found`. `replace` (default true) clears '
        "the field first; false appends. `submit: true` then presses the keyboard's submit key. `force` is "
        'as for `tap`. Answers `Typed into <element>. Value is now "…"` and what changed.'
    ),
    'scroll': (
        'Scroll content: `down` shows what is below. `direction` (default `down`), `amount` (0.1-1.0 of '
        'the scroll area, default 0.8), `to` (`start` or `end`, repeats until nothing changes; or `target`, until the `target` element is on screen and clear of the bars) and '
        "`target` (the scroll view, or an element inside it; default the screen's main scroll view) shape"
        ' it. Answers what changed.'
    ),
    'toggle': (
        'Set a switch or checkbox. `target` picks it; `value` true or false sets it (nothing happens if '
        "it's already there); omit `value` to flip it. Answers the new state and what changed."
    ),
    'back': (
        'Go back one screen. An open sheet or popover closes first, where it can be closed. Answers what '
        'changed; `not_actionable: no_action` when there is nothing to go back to.'
    ),
    'open_url': 'Open a URL or deep link, like "myapp://settings". Answers what came up and what changed.',
    'wait': (
        'Wait `seconds` (0.1-30, required) for an animation, a load or navigation, then answer what '
        'changed. Out-of-range values are `bad_params`. Answers `Waited 1.0 s.` and the changes.'
    ),
    'wait_for': (
        'Wait until something shows on screen: one of `text`, `id` or `label`; with `gone: true`, until '
        "it's gone (a spinner). `timeout` is 0.5-30 s (default 10). Returns as soon as it's true, with "
        '`Done after 1.2 s.` and what changed, or `timeout`.'
    ),
    'apps': 'List the apps you can drive, with the current one marked and the ids `open_app` takes.',
    'open_app': (
        'Drive this app from now on. `app` is an id or a name (names match ignoring case). Several '
        'matches give `ambiguous` with their `apps` lines.'
    ),
    'viewer': (
        'The link to the live view, where the user can watch the app as you work and take over. Their '
        'touches go first, and they can pause you. Give the link to the user only.'
    ),
}

EXTRA = {
    'run': (
        'Run several steps in one call, each an ordinary tool call written as {"tool": "tap", "target": {...}}, with '
        'an optional "expect" ({"text": "Saved"}, or id or label; with "gone": true, a "value", a "timeout" in '
        'seconds, default 2) checked after it. It stops at the first step that fails or whose expectation does not '
        'hold, and answers once: how many steps ran, then what changed since your last list, then the notes of each step. '
        'Use it for a known sequence (fill a form, then submit and expect the result): one call instead of five. '
        'trace: true adds the first line of each step.'
    ),
    'record': (
        'Save what you do as a script that replays without an agent: record start: true (optional name), act as '
        'usual, then record stop: true with a path. Each action that succeeded becomes a step, its target saved as '
        "the element's id, else its label, else its text."
    ),
    'replay': (
        'Run a saved script (from record, or written by hand in the same format) like run: it opens its app fresh '
        'first and stops at the first step that fails. People run the same file in CI with `tapwright replay FILE`.'
    ),
    'audit': (
        'Check the screen in front for accessibility problems and get a report: low contrast, tap targets smaller '
        'than 44x44 pt, elements with no useful label, text that is cut off or does not grow with the text size, '
        'wrong traits, and duplicate ids. Each issue names the element and says how to fix it. `types` narrows it '
        '(contrast, hit_region, description, dynamic_type, clipped_text, trait, element_detection, duplicate_id). '
        "Runs Apple's own XCTest audit on the phone (iOS 17+); it can take a few seconds."
    ),
    'look': (
        'reads the whole phone; the header may add `Keyboard:`. ocr: true also reads the text in the picture that no '
        'element carries (a game, a canvas, a web view), as lines o1, o2... that a touch takes as a target ref.'
    ),
    'tap': (
        "makes a real touch at the element's centre, or at the centre of its visible part when a bar "
        'covers the centre. `dry_run: true` says what it would tap and whether a person\'s OK is needed, touching nothing.'
    ),
    'long_press': 'holds for `seconds`. The policy and `dry_run` are as for `tap`.',
    'type_text': (
        'taps the field and waits until it has focus and the keyboard is up, then types on the keyboard. '
        "It can't see which field is focused, so without a target it uses the only input on screen. A "
        "secure field's value is reported as hidden. `dry_run: true` says what it would type into (only how many characters), typing nothing."
    ),
    'scroll': (
        "drags on the screen; the main scroll view is the one at the screen's centre. `to: \"target\"` drags "
        'toward the element until it is clear; a row a lazy list has not made yet is `not_found`. `dry_run: true` says what it would do, scrolling nothing.'
    ),
    'toggle': (
        'reads the switch, then taps it only if the value differs. With `look: false` the first line is '
        "`Tapped …`: the new state isn't read. The policy and `dry_run` are as for `tap`."
    ),
    'back': (
        "closes an open popover with a touch outside it; else taps the navigation bar's back button (never "
        'a Cancel, Close or Done), or swipes from the left edge when there is no navigation bar; '
        '`no_action` when nothing changed. `dry_run: true` says which of these it would do, doing none.'
    ),
    'open_url': 'opens it on the phone, so another app may take it. The answer says which app came up.',
    'wait_for': 'polls the screen, so it is slower; `network_idle` is `unsupported`.',
    'apps': 'the apps installed on the phone, with bundle ids.',
    'open_app': 'launches the app or brings it to the front, and answers the first screen.',
    'viewer': 'opens in a browser; shows the whole phone.',
    'close_app': (
        'Quit an app, so it starts fresh next time. `app` is a bundle id or name. Use it with `open_app` '
        'to start a clean run.'
    ),
    'alert': (
        'Answer the system alert on screen: `accept` (the default or allow button), `dismiss` (cancel or '
        "don't allow), or `text` (read it). Answers what changed. Without an alert, `not_found`."
    ),
    'press_button': (
        'Press a hardware button: `home`, `volumeUp` or `volumeDown`. `home` leaves the app, which keeps '
        'running; `open_app` brings it back.'
    ),
    'swipe': (
        'Drag from `x0, y0` to `x1, y1`, in points, over `seconds` (default 0.35). For sliders, '
        'carousels, swipe actions and edge gestures; for lists, use `scroll`. `dry_run: true` says what it would drag, touching nothing.'
    ),
    'rotate': (
        'Turn the phone: `to` is `portrait`, `landscape_left` or `landscape_right`. For testing how an app '
        'looks and works in landscape. Answers with the whole screen in its new size; a screen that only '
        'supports portrait stays as it is, and the answer says so.'
    ),
}


def describe(name: str) -> str:
    """A tool's description: the shared paragraph, then on the next line Tapwright's own line, labelled as in
    the shared text. A tool only Tapwright has is just its paragraph; one with no Tapwright line, the shared one."""
    if name not in SHARED:
        return EXTRA[name]
    return SHARED[name] + ("\n" + "Tapwright: " + EXTRA[name] if name in EXTRA else "")
