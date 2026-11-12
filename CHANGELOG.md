# Changelog

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
  check writes its picture to `<script>.diff/`, and the JUnit failure names it. References and pictures are written
  whole or not at all, and never through a link: a symlinked folder is refused, a symlinked file is replaced. A picture
  far larger than any screen, or one a script's reference would stretch to such a size, is refused before it is
  resized.

**Watching motion**
- `filmstrip`: several frames in one numbered picture, for animations and loading states, taken from the live
  stream. With `tap`, frame 1 is taken before the tap and the other frames follow it, so the strip shows what the tap
  sets off.

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
