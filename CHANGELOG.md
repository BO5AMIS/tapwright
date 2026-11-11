# Changelog

## 1.1.0, "the Autopilot Update" (unreleased)

Agents get more done per call, a session becomes a test you keep, and every touch that matters asks you
first. The interface is AGENT-TOOLS 1.8, shared with Tapline.

**Do more per call**
- `run`: several steps in one call, each an ordinary tool call with an optional `expect` (text, id or
  label appears or goes; a value; a timeout). It stops at the first step that fails, says which, and keeps
  that step's own error code; a missed expectation is `expect_failed`. One answer for the whole batch.
- `dry_run: true` on a touch tool or on `run`: what it would do, the safety policy included, without touching.

**From a session to a test**
- `record` saves what the agent does as a script (the agent-tools script format Tapline shares); `replay`
  runs it again.
- `tapwright replay FILE… [--junit OUT]` runs scripts with no agent, for CI: each reopens its app and
  waits for it, exit 0 when all held, 1 when one stopped, 2 when one couldn't start.

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

**Live view**
- Every control by keyboard with a visible focus ring; keys on the page's own controls never reach the
  phone; screen readers hear each step; high contrast and reduced motion; the confirmation prompt.

## 1.0.0

The first release: an agent sees and drives a real iPhone from Windows, Linux or macOS, through
WebDriverAgent and a developer tunnel, over USB or Wi-Fi; the live view; personal mode.
