# Changelog

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
