# Changelog

## 1.6.2 (2026-11-14)

- A WebDriverAgent session re-created in the middle of a run now gets the same settings as the first one (no waiting for
  animations such as a blinking caret, and the stream's current quality) right after it starts. Before, a tap and then
  typing after a re-created session took 3.7 to 5.0 s; now it takes 1.7 s. A quality change keeps the requested settings if the connection drops and is
  retried.
- `alert` takes only `accept`, `dismiss` or `text`. Any other action is refused as `bad_params` and names the allowed
  values; nothing from the call is put into a URL.
- Typing into a field that already has focus can still open iOS's caret menu; a fix that reads focus from the runner
  comes in 1.7.

## 1.6.1 (2026-11-14)

- A place is `place_<n>` until a person names it. Tapwright no longer names a place after a repeated navigation title
  (a private title such as "Chat with Sara" would have become a stored place name). Maps that already hold
  title-derived names keep them. Same rule as AGENT-TOOLS 1.13.2, §18.1.
- Tapwright never names a place after a navigation title, and learned roads keep no title in plain text (a native Back
  is learned as `back`). Scripts, routes and reflexes you record or define keep what you recorded.
  A native Back button is labelled with the title of the screen under it, so a road learned from a tap on it is `back`
  with no name; a road that would aim by a title (or keep one among its other names) keeps none of it. Whether a tap was
  on the Back button is decided before the screen is read again, so a screen that comes back with another title (a chat
  renamed) doesn't hide it, and only a tap on it is learned as `back` (a long press is not). A recorded script or route
  keeps its steps, waits, expectations and `ignore` entries as recorded, a title included, so it replays the same way.
- `crash` with `pull` also replaces a number, true, false or null in the report's JSON whose text is a test account's
  value (a PIN, a user id) with the placeholder, and checks the finished file; a report that still holds a value is not
  saved, and the answer says so. A road repaired during `drive` keeps none of the titles the screens showed on the way.
  A recorded step aimed by a placeholder is saved from the element the action really touched (a toggle's switch).
- A test account's password or username that a screen shows (a revealed password) is now replaced by its placeholder
  (`<tester2's password>`) in everything that leaves Tapwright, in any form (escaped, collapsed, cut): text answers and
  errors, change lists (a `(was …)` cut no longer keeps a password's end), alerts, the audit's element lines (`id=`) and
  XCTest's words, OCR lines, the live view's captions and confirmation requests, the host's OK dialog, recorded scripts
  and routes, maps (ids and title hashes), journey reports (`report.md`, `report.html`, `journey.json`, an exported
  issue's title) and the action log. Each is redacted before it is cut.
  Inside, Tapwright keeps the screen's own words, so the safety policy, typing and matching work as they do without
  accounts: a password `Delete` doesn't hide a Delete button (or an alert's Delete) from the confirmation or a block
  rule, and replacing a field's text erases all of it, not as many characters as its placeholder has. A target or an
  expectation matches the screen's words or their placeholder, so `{"text": "<tester2's username>"}` finds what an
  answer showed that way, and a script that names the value itself still replays.
  A recording never saves a selector that holds a value or its placeholder: it takes the id, else the label or text,
  else the point of the element the touch really reached (not of the list the agent had before the call, which may be
  empty before a first look). A step that can't be saved that way, or a wait or expectation on a value, refuses the save
  with a message that names the step and never the value; nothing is saved and no step is dropped without a word. A
  `compare_screen` ignore that holds a value is saved by the element's id or rectangle. As a script or route is written, only its content (labels, texts, ids, values, links) is redacted,
  never a key, a tool, the format or a `${account.…}`, so an account whose username is `tap` leaves the saved tool
  `tap`. A redacted file is checked as `replay` reads it before it is reported saved.
  Every sink is guarded where it writes, so a path that was missed can't leak: a diagnostic (an app's log line, a
  crash reason, the logs a failed step carries) is redacted before any length cut, and a value is never kept in the log
  ring; the daemon's log (`serve.log`) redacts every message and traceback with the accounts the daemon has loaded; a map
  holds no account value (the selected tab's id, a repaired road's target; the writer drops a road or replaces a value
  that still holds one, never a key); and a value that the screen's text shows without its invisible direction marks
  is redacted in that form too.
  Every shape of a value is redacted, and `holds` knows each. What is guaranteed: a value is found as it is; with
  whitespace normalized (any run of whitespace or `+` is one space, the ends may be trimmed) and direction marks
  anywhere; percent-encoded (`%HH` in either case, UTF-8); with JSON or Python escapes (`\uXXXX` and surrogate pairs,
  `\UXXXXXXXX`, `\xHH`, `\\`, `\"`, `\'`, `\/`, `\b`, `\f`, `\n`, `\t`, `\r`); and any composition of those, each
  layer a whole-text encoder (a `%r` inside a JSON crash report, a URL inside a JSON string) or a different escape per
  character, decoded pass by pass to convergence. Only those escapes are decoded: any other backslash is a literal
  character, so a password `pass\word` doesn't hide the word `password`. Every occurrence at every level, overlapping
  ones included, is mapped back to the characters it came from and redacted whole, and a log message, a traceback or a
  report shows none. Decoding stops at a work limit of 16 passes; a whitespace-separated token that
  would still change is redacted whole as `<a value encoded too deep to check>` and counts as holding a value, so
  nothing is released unchecked. Searching is linear in the text and the values. A username or password is 3 to 256
  characters (at most 256 as written, at least 3 once decoded and trimmed, so three spaces are refused) and must settle
  within the limit, and a file holds at most 32 accounts (64 values); a file that breaks this is refused when it loads,
  naming the account and the field (or the number of accounts), never the
  value. Not covered: other encodings (base64, HTML entities, a hash, a change of case, a value split by other text), and a
  backslash left bare right before an escaped character (`\` + `\x73`), which JSON and Python, and the matcher, read
  as an escaped backslash; and text around a value that ends or starts with an incomplete escape (`%2`, `\`, `\u00`)
  and so is decoded together with the value's first or last characters (boundary fusion), which the matcher catches
  in the common cases as a best effort, without a guarantee.
  A route's declared variables are filled in every string field and an undeclared `${name}` is literal text in every
  field, typed text included; a literal `${` is written `$${` in every field the recorder saves (in the copy it writes,
  so a save that fails and is tried again escapes it once), so a replay taps the element that was recorded; the save
  reads every field's literal runs decoded. A value ever
  loaded from any accounts file stays redacted for the daemon's life, even after the file changes (typing uses only the
  current ones). A crash report saved by `crash` with `pull` (and the copy `replay` puts beside a failing script) is a
  sanitized copy in the same format: each JSON document (or JSON line) is parsed and every decoded string scrubbed, so
  a value its bytes hold only escaped is caught too. The map writer redacts only content fields (never the format, a hash, a key or a
  name), and a redacted map that wouldn't read never replaces the file.
  Only escapes are case-insensitive; the value's own letters keep their case.
  A recording refuses to save typed text that holds a value from before an accounts-file reload, as it does for a current
  one, and checks typed text again at the save against the file as it is then.
  Typed account values are recorded as `${account.username}` / `${account.password}` (text that is exactly the value, a
  username in a text field, a password in a secure one); text that only holds a value is refused naming the step. A
  recorded selector is counted and checked the way the replaying tool resolves it (a toggle counts switches first), and a
  toggle that changes nothing is still recorded by its switch.
- `tapwright call TOOL -` reads its JSON from standard input as UTF-8 on every system. On Windows it was read in the
  console's code page, so Arabic or Japanese piped in was typed as mojibake. Input that isn't UTF-8 is refused, not
  sent.
- QUALITY-BAR 2.5 (`docs/QUALITY-BAR.md`).
- The spec is AGENT-TOOLS 1.13.2 (`docs/AGENT-TOOLS.md`, the same bytes as Tapline's). Besides §18.1, it says that
  pictures are free of a password only while its field stays secure (§18.6), adds `"by": "chat"` to `confirm_closed`
  for an OK given in the chat (§11.3), and marks the passcode unlock (`phone.passcode`) as planned.

## 1.6.0, "the Speed Update" (2026-11-13)

An agent on the phone should feel almost instant. A real run (10 workers, 760 calls) showed the largest cost was
outside the tools (a new process for every call), then the number of decisions, then the size of each. 1.6 goes after
all three, with no model inside Tapwright and no API key. On ground the tool knows, no model is asked anything; on new
ground a small, fast model drives and the tool remembers the way. The interface is AGENT-TOOLS 1.13, shared with
Tapline.

**A call without the plumbing**
- `tapwright call TOOL '{json}'` runs one tool in the running daemon and loads only Python's standard library, for a
  host that can't keep an MCP connection (`--apps` or `$TAPWRIGHT_APPS` for the scope, `--client`, `--json`; exit 0,
  2 for a refusal, 3 when the daemon can't be reached). Loading the MCP server once is still the fastest route.
- `tapwright serve --apps A,B` (or `--dedicated`, `--policy FILE`) is a ceiling: no call, MCP or thin, may name
  more apps than that or drop a word from the policy.
- `session_report` counts seconds, not only calls: the agent's thinking time between calls, the tool's time, by tool,
  and a line when every call came from a new process.

**Shorter answers, fewer calls**
- `brief: true` on any action, `run` or `drive` (`tapwright mcp --brief` for all): the first line, at most 12 lines
  of changes, then `…<n> more: look shows them.` A new screen lists its actionable elements and rows only.
- Steps gain `if` (do this step only when that is on screen) and `optional` (skip it when its own target isn't
  there), in `run`, scripts and routes, so one decision covers a flow with a maybe-screen. Time spent waiting for a
  person doesn't count toward a batch's 120 s.

**The map, and `drive`**
- Tapwright recognises a screen again whatever its content or language (a signature of the fixed elements, with
  the title kept only as a hash) and saves each move between screens as a road, in one readable file per app
  (`~/.tapwright/maps/<bundle id>/map.json`, or `--maps DIR` to share it with a project and with Tapline; two tools
  can write one file, merged on load). A list row's text, typed text and secure values are never in it.
- `map` lists the places, roads, routes and reflexes, shows the way to a place, and names places, adds a place's
  link, adds and removes reflexes, and forgets a place. Every full answer says `Place: <name>.`
- `drive` goes to a place by a link or the shortest chain of proven roads and answers once (`Arrived at language
  by 3 taps in 1.9 s`). It repairs a stale road by another name of the same element, plans again from another
  known place, and otherwise stops with the new error `off_route`, having touched nothing more. It never claims an
  arrival it didn't check, and every move passes the same scope, policy and confirmation as a single call.
- Routes: `record` with `route` saves a flow that starts from a named place and takes variables (script version
  2); `drive` runs it with `with`, `account` and `then` (and `by`: `any`, `taps`, `link`).
- Test accounts: `--accounts FILE` keeps named accounts on the user's computer. The agent says `account: "tester2"`
  and the tool types the values, a password only into a secure field. They are never in an answer, log, map, script or
  report, and a file inside a git work tree that git doesn't ignore is refused.
- Reflexes: the tool waits out a loading indicator before calling a target `not_found`, moves the keyboard out of
  the way, and runs the reflexes the user or agent declared for the app (one step each, never an action the policy
  flags), saying every firing.
- `alert` answers an alert that another app draws about a given app (Settings asking `Delete "Wayfare"?`), under the
  policy; a risky answer still asks a person.
- A journey in a user's eyes has no jumps, reflexes or shortcuts unless it allows them (`allow`: `drive`, `reflexes`,
  `fast_animations`).

**Agreed with Tapline after AGENT-TOOLS 1.13 (the 1.13.1 clarifications)**
- `off_screen` now means no scroll can bring the element on screen (a touch scrolls to an off-screen target by itself),
  and says to look, then target something else or report it.
- A script, reference picture, route or place name is ASCII, and one that differs from an existing name only in case is
  refused (`bad_params`, naming the existing one) before anything is written.
- A double-tap stress sends no second touch when the control's id, label or text changed after the first: it answers
  `changed`.
- A route's drive line is `Arrived at home by route sign_in in 3.2 s.`, and `session_report` flags five thin calls in a
  row, and a retry after an id's `not_found` says to target what the latest look shows.

**A driver for new ground**
- `tapwright install` and `tapwright skill install` write the driver agent `phone-driver`: a subagent on the host's
  fastest small model (Claude Code: `.claude/agents/phone-driver.md` on Haiku, low effort, the driving tools of
  Tapwright and Tapline; Codex: `.codex/agents/phone-driver.toml` on `gpt-6.1-terra`), with a short handbook:
  `map` first, `drive` when the map has the way, several moves in one `run`, read every answer whole, stop on
  `needs_confirmation`, `locked` or `paused`, and finish in five lines. `--driver-model NAME` changes the model.
  For a host with no agent files the handbook is `references/driver.md` in the skill, to paste as a brief.
- The skill's "Driving fast" gives the main agent its rules: known ground is one `drive`, unknown ground goes to
  the driver (one for the session), accounts by name, never step by step.

**A person's OK in chat, and fewer hands on the phone**
- A touch that needs a person's OK (Buy, Delete, Send...) is asked in the host's own dialog (MCP elicitation) as
  well as in the live view: Allow once or Don't allow, the first answer wins, and the screen is read again after an
  allow. `--no-dialog` turns it off for a host that can't show it. The agent can never answer it.
- A locked phone, the XCTest passcode prompt, iOS's Health sheet and an iOS alert are asked of the person the same
  way, and the call goes on by itself when it clears (up to 120 s).
- `tapwright doctor --unattended` says what would stop a run with nobody there (the phone locked with no passcode
  given, Enable UI Automation off, the computer not trusted, Developer Mode off, a low battery) and how to fix each.

**Faster touches, and a more honest read**
- The runner on the phone is now the fork `tw-16.15.3.4` (Appium's 16.15.3 with Tapwright's changes). It reads the
  screen in a compact form and checks which app is in front inside the tap itself, so a tap needs fewer round trips.
  `tapwright setup` installs it.
- The wait for a still screen is cut when the live picture already proves the screen is still, so a read right after
  a tap doesn't wait again.
- `scroll` with `to: "end"` or `"start"` sends the drags one after the other, then waits once and reads once, where
  it waited and read after every drag.
- `back` always checks that something changed, with or without a `look`, by the picture before and after and by the
  tree. When nothing changed it answers `not_actionable: no_action`, as before.
- A tap on an element that another view is drawn over is refused `covered`, as a person's finger would be stopped,
  even when that view isn't in the list (a named plain view over a button). `force` still works. iOS's scroll bars
  never count as covering.
- Elements that lie wholly off screen to the left or right (the screen before, kept under a stack navigator) are no
  longer listed.
- The note after a touch that changed nothing names the touch: `the tap was delivered…`, `the long press was
  delivered…`, `the double tap was delivered…`.
- `TAPWRIGHT_TRACE=1` writes one line of timing for each touch and each read to `~/.tapwright/trace.jsonl`, for
  finding where a slow call spends its time. Off by default; no screen content is written.
- Fixed: `type_text` and `drive` failed through MCP in a 1.6 build. A typing that doesn't land says why in the trace.

**Measured** (the same iPhone 14 Pro Max, Wayfare's benchmark lab, Tapline's runner, 1.0.0 against 1.6.0, over USB):
a tap 0.74 to 0.61 s, a toggle 0.60 to 0.42 s, a scroll by a real touch 2.05 to 1.64 s. Misses in the touch checks
(R1, R2) went from 6 to 3 of 48, and a tap on a covered element is now refused. Typing and a text look are the same
as in 1.0.0, and closing and opening a development build are not faster. Wi-Fi varies from minute to minute, and
scroll and typing over Wi-Fi came out slower in this run. The tables and the notes are in
[docs/benchmarks.md](docs/benchmarks.md).

**Not in 1.6:** fast animations, the Safari and web-page tools, the Wi-Fi radio work, unlocking the phone with a given passcode and the starter map for iOS Settings move to a later update.

## 1.5.1 (2026-11-13)

Small fixes from the first real-app runs. No new tools.

- `close_app` is faster. It ends the app through iOS's own process control, then tells WebDriverAgent, which waited a
  median of 46 s for a real app to finish quitting. The answer is the same, and a slow or failing second step is
  logged, not waited for. Without the tunnel, if iOS refuses, takes over 5 s or finds no process to end, it falls back
  to WebDriverAgent alone as before; `TAPWRIGHT_CLOSE=wda` forces that. `open_app` with a language, region or text size
  closes a running app the same way. A bundle id nothing has gives `not_found`.
- A `tap` or `long_press` that changes nothing keeps `No visible change.` and adds a note: the tap was delivered, and
  nothing on screen changed within N s, with what to try (a text field: `type_text`; a point with no element; else
  `wait_for` what you expect, then report it as a finding). Agents couldn't tell whether the touch landed, so they
  looked again and retried. iOS's Health access sheet keeps its own note.
- The skill and the agent guide say four things more: read every answer whole (never pipe a call through `head`,
  `grep` or `sed`), send known steps as one `run`, ask for the app's deep links and use them with `open_url`, and
  connect once as an MCP server (a wrapper that starts a new process for every call adds about 2 s to each call).
- React Native radio and checkbox options are listed, with their checked state (`radio "Daily" checked=true`). iOS
  gives them no accessibility trait, so they were typed `Other` and left out of `look` unless they had an id. They are
  found by React Native's role words in the label or value (`radio button`, `checkbox`, English only), so no other
  `Other` changes: a card, a scroll indicator or a plain labelled view is listed as before. `audit` flags each such
  element once: no trait, so VoiceOver doesn't announce it as a control.
- The first touch by id, label or text after `open_app` reads the screen again before it aims. The list `open_app`
  answered with can miss a search field drawn a moment later, and a row under it was touched at its centre, beneath
  the field. The row is now touched at its visible part.
- A plain `open_app` after one with a language, region or text size says what it undid:
  `Opened Example (com.example.app), back to the phone's own text size.`
- `media`: the first add, which makes iOS ask the person whether the runner may add photos, answers `needs_a_person`
  at once. Tapwright never answers that prompt itself. The runner can do nothing else until the person answers (two
  minutes at most), so meanwhile other calls answer `busy` right away instead of waiting, and Tapwright doesn't take
  the silence for a lost connection. The add finishes once the person answers, and calling `media` again with the
  same pictures picks it up rather than adding them twice.
- The journey's stop line names the errors its report counts: `Findings: none. Errors: 2 (1 not_found, 1 covered).`
- The live view's link with its key is never printed (the start line of `serve`, "already running", and `view`). An app
  around the terminal had copied such links with the key hidden as `?token=SECRET` and opened them as tabs, which
  could only fail. `viewer` leaves an open live view alone instead of opening another tab (`health` reports the
  connected pages), and no test opens a real browser.

## 1.5.0, "the Real User Update" (2026-11-13)

Apps are made or broken by whether a user can find a feature, understand it and finish it easily. An agent can now
test that, going through a feature as a real user would, and file a visual report. The interface is AGENT-TOOLS 1.12,
shared with Tapline.

**A journey, in a user's eyes**
- `journey` with a `goal` ("log breakfast by scanning its barcode") and, if useful, a `persona` and `conditions`
  (language, region, text size, network). While it runs, the agent sees what a user sees: no ids, nothing off screen,
  no jumping to a target, and icon-only buttons shown as such, with what VoiceOver says kept apart.
- The tool measures the trip: taps, screens, typing, scrolling, time (waits apart), wrong turns, dead ends, errors,
  hesitations, and the taps to the goal.
- Stress moments: leaving the app and coming back, a bad network mid-flow, a double tap, rotating, relaunching, each
  checking that the user's progress survived.

**Personas as guidance, never enforced**
- The skill shows how to build personas for the app at hand, with example personas to copy, extend or ignore (the same
  file as Tapline's). No setup is required: a journey can start with only a goal.

**Each screen against the basics**
- `ux_check`: competing choices, one clear main action, thumb reach, icon-only buttons, cut-off text, contrast, target
  size, naming that changes between screens, and error or empty states that don't say what to do. Facts first, each
  naming its element and basis; the agent adds its own judgment and says it is judgment.
- House rules (`--ux-rules FILE`): the user's own taste, as text the agent judges, or as rules the tool checks
  (`max_taps_to_goal`, `way_back`).

**A visual report**
- When a journey stops, a report folder (Markdown, HTML, screenshots, a filmstrip, marked-up findings, and a GIF of
  the journey from the live stream), with its summary in the answer and at most two key pictures. `export` puts it in a
  GitHub PR or issue, only when the user asks.

**Test pictures for photo pickers**
- `media` adds pictures to the phone's photo library for an app's photo picker, after the user's OK, and removes only
  what it added. It needs the runner this release pins, tw-16.15.3.3, which has the photo routes (`tapwright setup
  --force` installs it); an older runner answers `unsupported` and says so.

## 1.4.0, "the Fewer Steps Update" (2026-11-13)

The same check should cost an agent no more calls on the iPhone than on Android. A QA pass found iPhone lanes took
about 1.5 times the calls: failed targets, scrolling to search, detours through iOS Settings, and a `look` after every
action. The interface is AGENT-TOOLS 1.11, shared with Tapline.

**See where the calls go**
- The action log now records reads too, which agent made each call, and how long each answer was (never typed text,
  pictures or log text).
- `session_report` (and `tapwright report`): calls per tool, failures by code, and the waste to cut, with call numbers:
  a `look` right after an answer that already had the changes, retries after `not_found`, scroll hunts, and actions
  that could have been one `run`.

**One call where it took several**
- `tap`, `long_press`, `toggle` and `type_text` on an element that is off screen scroll it into view and act, in one
  call (`Scrolled to it, then tapped …`). The safety check runs before the scroll.
- `not_found` names up to 3 closest matches (`Closest: e12 button "Save draft" id=save-draft, …`).

**No more trips through Settings**
- `open_app` takes `language`, `region` and `text_size` (`xs`…`xxxl`, `ax1`–`ax5`, `system`): the app opens as if the
  phone were set that way, for that launch only, through Apple's launch arguments. Nothing in Settings changes.
- `network`: `offline`, `3g`, `lossy` or `normal`, through the phone's own network conditioner. It affects the whole
  phone, every answer says so while it's on, and it goes back to normal by itself when the agent that set it goes
  quiet, the connection drops or the daemon stops. Over Wi-Fi, `offline` and `lossy` are refused, as they would cut
  Tapwright's own link.

**Fixes from the review of this release**
- A secure field's text never reaches a recording, even when the field was below the screen: the step is saved from the
  field `type_text` really typed into, after any scroll.
- A `network` condition lives only as long as the agent that set it. The agent's MCP process sends a heartbeat every
  few seconds and a goodbye when its stdio closes; the condition is cleared on the goodbye, 20 s after the heartbeat
  stops (the process was killed), after 10 minutes without a call, and at the 30-minute cap. A condition whose
  connection to the phone closes is noticed at once. A daemon that starts and finds a condition left by one that
  crashed clears it before any agent's first call.
- One agent never clears another's condition by accident: a failing `run` clears only the condition it set, a
  replaced or ended condition is told to its owner in its next answer, and a goodbye from one agent leaves the
  other's condition alone.
- `network` checks a per-app policy against the app in front now, not the one at the last read, and refuses when the
  phone can't say which it is and a policy blocks `network` for some app.
- A plain `open_app` (or `text_size: system`) after one with a language, region or text size closes the running app
  and opens it with the defaults, and says so.
- `tap`, `long_press`, `toggle` and `type_text` with an `index` on an off-screen match follow that element through the
  scroll by its own identifier (when nothing else in the tree has it) and act on it, never on another match, and the
  policy checks the element that is touched. A match without an identifier of its own is scrolled into view and not
  touched (`stale`: look, then target it by its ref): as a list scrolls its matches move, drop and change, and an
  `index` can then name another one.

**Habits that save calls**
- The skill has `run` examples for common patterns and a "spend fewer calls" list: read the change list instead of
  calling `look`, target by id, let the action scroll, and put known steps in `run`.
- The shared benchmark (QUALITY-BAR 2.4) adds calls per check: five everyday checks on Wayfare, held to no more calls
  than Android.

## 1.3.1 (2026-11-13)

Small fixes from an app tester's pass.

- `tapwright doctor` checks where Tapwright runs from. In a git checkout with uncommitted changes it warns; with
  unresolved merge markers (`<<<<<<<`) in the package it fails. An installed package is fine, and no git means no
  check. The setup docs and the agent guide now say that QA and unattended runs use a pinned release in their own
  venv, never a development checkout.
- A "needs a person" list: `doctor` shows, under `Needs a person:`, what only the phone's owner can clear and can
  be read (the XCTest passcode prompt, a locked phone, Developer Mode off, a computer the phone doesn't trust), with
  what to do. The first answer each agent gets carries the same as `Note:` lines.
- A tool called with an argument it doesn't take is now refused over the HTTP API too: `bad_params: audit doesn't
  take labels, ocr; it takes types.` The same holds for the steps of `run` and scripts.
- `audit`: a finding whose element isn't in the screen's tree is named by XCTest's own label or id and frame
  (`an element "Total" at @16,300 120x20`) before it falls back to "the screen".
- The live view's page for a link without a valid key now says what to do (run `tapwright view`, or ask the agent to
  use its `viewer` tool), instead of one line a person couldn't act on.

## 1.3.0, "the Diagnostics Update" (2026-11-13)

A failure now comes with its cause: the app's log, its crash, and how it performed. The interface is AGENT-TOOLS
1.10, shared with Tapline.

**The app's log**
- `logs`: the app's own entries in the phone's unified log (os_log, NSLog, and React Native's console in a release
  build), from the app's process only, never another's. `level`, `grep`, `limit` and `since` narrow it; each answer
  ends with `Latest:` to pass back as `since`.

**Crashes**
- When the app crashes during a call, the answer says so in one line (`Note: the app crashed: …`), on error answers
  too; a crash found later is told once on the next answer, with its time.
- `crash`: the app's crash reports on the phone since the session began: when, the exception and the top frames,
  the app's own marked. `pull: true` copies the full report to `~/.tapwright/crashes/`.

**Performance**
- `performance`: memory and CPU of the app's process and the screen's frame rate, while waiting, around a `tap`, or
  from a cold start to the first settled screen (`launch: true`).
- In `run` and scripts a `performance` step can hold limits: `"expect": {"under": {"launch_s": 2, "memory_mb": 300,
  "drops": 0}}`.
- A limit of its own: a batch of samples already in flight when the window opens carries no timestamp, so it can't be
  told apart from the window's own and can skew a number at the very edge of the window. Samples are counted by when
  they arrive, not by when the phone took them. Samples still waiting when the window ends, or lost to a process list
  that couldn't be read, are said, and fail memory and CPU limits.

**Failures explain themselves**
- A `run` or `replay` step that fails adds the app's warnings and errors since that step began. `tapwright replay
  --junit` puts them in the test case's output, and pulls a crash report next to the script.

## 1.2.0, "the Regression Update" (2026-11-12)

A test now checks how screens look, not only what they say. The interface is AGENT-TOOLS 1.9, shared with
Tapline.

**Visual checks**
- `compare_screen`: the first call for a `name` saves the screen as a reference picture; later calls answer
  `Matches` or `Differs`, with how much changed and where (each region named by the elements it touches), and on a
  difference a picture with the changes in red. `threshold` is the percent that may change and still match
  (default 0.5); `ignore` paints out elements or areas that always change. The status bar is never compared.
- In `run` and scripts it is a check: `expect: {"matches": "home"}` on a step, or a `compare_screen` step. A screen
  that differs stops the batch as `expect_failed`, naming where it changed.
- `record` folds a `compare_screen` after an action into that step, and saves the reference pictures next to the
  script in `<script>.references/`, so a script and its pictures travel together. A `run` made while recording keeps
  its visual checks too (`expect.matches` and `compare_screen` steps). A script keeps one picture for each name, so a
  recording that compares one name in two apps is refused.
- `tapwright replay` takes `--update-references` to save new references after an intended design change. A failed
  check writes its picture to `<script>.diff/`, and the JUnit failure names it. A picture
  far larger than any screen, or one a script's reference would stretch to such a size, is refused before it is
  resized. A recording refuses an update that would change a picture it already compared, before anything is written.
- The safe write covers the reference pictures (the store's and a script's `.references/` folder) and a script's
  `.diff/` folder, and nothing else: they are written whole or not at all, and never through a link (a symlinked
  folder is refused, a symlinked file is replaced). On Linux and macOS every folder is held open while the file is
  written. On Windows the folders are checked again before and after the move, which narrows the window for a folder
  swapped for a link, but can't close it: a process running as the same user that swaps a folder at that very moment
  can still get a write through.

**Watching motion**
- `filmstrip`: several frames in one numbered picture, for animations and loading states, taken from the live
  stream. With `tap`, frame 1 is taken before the tap and the other frames follow it, so the strip shows what the tap
  sets off. A filmstrip is composed in memory. With `tapwright mcp --screenshots DIR`, filmstrips and the picture
  `compare_screen` returns for a difference are saved to that folder by the ordinary screenshot writer, which is
  outside the safe write above.

## 1.1.1 (2026-11-12)

- A touch that had to close the keyboard first failed with an internal error while a layer was open in front
  (an AutoFill menu, a sheet). Found by an app's release-candidate pass on the phone.

## 1.1.0, "the Autopilot Update" (2026-11-12)

Agents get more done per call, a session becomes a test you keep, and every touch that matters asks you
first. The interface is AGENT-TOOLS 1.8, shared with Tapline.

**A faster tap**
- Tapwright installs its own build of the on-phone runner (Appium's WebDriverAgent 16.15.3 plus a fast tap and drag,
  github.com/bo5amis/WebDriverAgent, checked by its SHA-256). Over USB a tap is 0.47 s (0.69 before) and a
  scroll 1.07 s (1.45); over Wi-Fi a tap and a toggle are within Wi-Fi's bar. `TAPWRIGHT_WDA=appium tapwright setup --force` goes back to
  Appium's runner. After upgrading, `tapwright doctor --fix` (or `setup`) sees the older runner and installs this one.

**Do more per call**
- `run`: several steps in one call, each an ordinary tool call with an optional `expect` (text, id or
  label appears or goes; a value; a timeout). It stops at the first step that fails, says which, and keeps
  that step's own error code; a missed expectation is `expect_failed`. One answer for the whole batch.
- `dry_run: true` on a touch tool or on `run`: what it would do, the safety policy included, without touching.

**From a session to a test**
- `record` saves what the agent does as a script (the agent-tools script format Tapline shares); `replay`
  runs it again.
- `tapwright replay FILE… (--apps ID[,ID] | --dedicated) [--junit OUT]` runs scripts with no agent, for CI: each
  reopens its app and waits for it, exit 0 when all held, 1 when one stopped, 2 when one couldn't start. The apps
  the scripts may use come from `--apps` (or `--dedicated`), never from a script's own `app`; with neither it
  refuses and exits 2.
- `tapwright install` takes `--policy FILE` and `--no-policy`. A rerun keeps the entry's `--policy`, `--no-policy`
  and `--screenshots`, and says so when it replaces a policy. The config file keeps its permission mode, and a
  symlinked config is written through.

**Find problems for the developer**
- `audit`: Apple's own XCTest accessibility audit of the screen (contrast, tap targets under 44x44 pt,
  missing labels, clipped text, text that doesn't scale, traits) plus duplicate ids; each issue names the
  element and the fix.
- `look` with `ocr: true` reads the text in the picture that no element carries (games, canvases, web
  views) as refs `o1`, `o2` a touch can take. Optional: `pip install "tapwright[ocr]"`.

**Safety**
- A touch on Buy, Purchase, Subscribe, Pay, Delete, Remove, Send, Post, Publish, Sign out, Log out or Erase
  needs a person's OK in the live view (Allow once / Don't allow). `tapwright mcp --policy FILE` sets your
  own words and blocks; `--no-policy` turns it off for a test phone with test data.
- A touch by position is refused as `stale` when the picture there changed since the agent's last look.
- The action log: one JSON line per action in `~/.tapwright/log/`, typed text never written, two weeks kept.

**Setup**
- `tapwright install --client claude|codex|cursor --apps …` connects an agent in one go: the MCP server and
  the skill.
- `tapwright doctor --fix` repairs the usual breaks (developer disk image, WebDriverAgent missing or its
  profile running out, the network pairing); `doctor` warns 30 days before the signing expires.
- `tapwright setup` run again skips what is done and ends with the next commands.

**Fixes**
- Closing the keyboard before a touch: the touch goes inside a list first, then a short pull on the list,
  and WebDriverAgent's slow attempt isn't repeated in an app where it failed. A control the keyboard still
  covers is refused with how to close it.
- A person's OK is taken only from the live view page, with request ids that can't be guessed.

**Live view**
- Every control by keyboard with a visible focus ring; keys on the page's own controls never reach the
  phone; screen readers hear each step; high contrast and reduced motion; the confirmation prompt.

## 1.0.0

The first release: an agent sees and drives a real iPhone from Windows, Linux or macOS, through
WebDriverAgent and a developer tunnel, over USB or Wi-Fi; the live view; personal mode.
