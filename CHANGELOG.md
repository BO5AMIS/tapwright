# Changelog

## 1.5.1 (unreleased)

Small fixes from the first real-app runs. No new tools.

- `close_app` is faster. It ends the app through iOS's own process control, then tells WebDriverAgent, which waited a
  median of 46 s for a real app to finish quitting. The answer is the same. Without the tunnel, or if iOS refuses or
  takes over 5 s, it falls back to WebDriverAgent alone as before; `TAPWRIGHT_CLOSE=wda` forces that.
- A `tap` or `long_press` that changes nothing keeps `No visible change.` and adds a note: the tap was delivered, and
  nothing on screen changed within N s, with what to try (a text field: `type_text`; a point with no element; else
  `wait_for` what you expect, then report it as a finding). Agents couldn't tell whether the touch landed, so they
  looked again and retried. iOS's Health access sheet keeps its own note.
- The skill and the agent guide say four things more: read every answer whole (never pipe a call through `head`,
  `grep` or `sed`), send known steps as one `run`, ask for the app's deep links and use them with `open_url`, and
  connect once as an MCP server (a wrapper that starts a new process for every call adds about 2 s to each call).
- React Native radio and checkbox options are listed, with their checked state (`radio "Daily" checked=true`). iOS
  gives them no accessibility trait, so they were typed `Other` and left out of `look` unless they had an id. `audit`
  now flags such an element: no trait, so VoiceOver doesn't announce it as a control.

## 1.5.0, "the Real User Update" (unreleased)

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
  what it added. It needs Tapwright's runner with the photo routes (runner version 3, not yet released); older runners
  answer `unsupported` and say so.

## 1.4.0, "the Fewer Steps Update" (unreleased)

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

**Habits that save calls**
- The skill has `run` examples for common patterns and a "spend fewer calls" list: read the change list instead of
  calling `look`, target by id, let the action scroll, and put known steps in `run`.
- The shared benchmark (QUALITY-BAR 2.4) adds calls per check: five everyday checks on Wayfare, held to no more calls
  than Android.

## 1.3.1 (unreleased)

Small fixes from a QA pass.

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

## 1.3.0, "the Diagnostics Update" (unreleased)

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

**Failures explain themselves**
- A `run` or `replay` step that fails adds the app's warnings and errors since that step began. `tapwright replay
  --junit` puts them in the test case's output, and pulls a crash report next to the script.

## 1.2.0, "the Regression Update" (unreleased)

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
  script in `<script>.references/`, so a script and its pictures travel together.
- `tapwright replay` takes `--update-references` to save new references after an intended design change. A failed
  check writes its picture to `<script>.diff/`, and the JUnit failure names it.

**Watching motion**
- `filmstrip`: several frames in one numbered picture, for animations and loading states, taken from the live
  stream; `tap` taps first and films what it sets off.

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
