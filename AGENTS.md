# Tapwright: guide for agents

You can see and operate the user's real iPhone through Tapwright's MCP tools, to test apps:
the user's own builds (TestFlight, development) or any installed app. This guide covers
setting Tapwright up for a user, then testing well and safely.

## Setting it up for a user

Do the computer side yourself; ask the user only for what needs their hands or their
Apple account. Follow [docs/setup.md](docs/setup.md).

1. `pip install tapwright`, then `tapwright doctor`. Fix
   what it reports on the computer (Windows: Apple Devices from the Microsoft Store).
   For QA or any unattended run, install a pinned release in its own venv
   (`pip install tapwright==<version>` or the wheel), never run from a development checkout.
2. Ask the user to plug in and unlock the iPhone.
3. Signing: ask for an App Store Connect API key (Key ID, Issuer ID, the `.p8` file's path,
   Admin role) and a bundle prefix they own (`com.theirname.tapwright`), or for their own
   `.p12` and profile. Never print or store the key's contents anywhere else.
4. Run `tapwright setup …`. Relay what the phone shows: **Trust This Computer?**, and if
   setup says so, turning on Developer Mode (Settings > Privacy & Security > Developer
   Mode, restart, confirm), then run setup again.
5. `tapwright smoke --app <one of their bundle ids>` proves the chain. If the phone shows
   **Enter iPhone Passcode for "XCTest"**, the user enters it; suggest Settings > Developer >
   Enable UI Automation, which stops the phone asking.
6. Add the MCP server to your host with the apps the user wants tested (Claude Code:
   `claude mcp add tapwright -- tapwright mcp --apps <bundle id>`). Without `--apps` every
   call is refused. `--dedicated` is only for a phone kept for testing. Connect once: load it as an
   MCP server when the session starts. A wrapper that starts a new process for every call adds
   about 2 s to each call.
7. `tapwright skill install` puts the Tapwright skill where Claude Code, Codex and other
   agents look for skills (`--project DIR` for one project). The skill is
   [skills/tapwright/SKILL.md](skills/tapwright/SKILL.md): how to test well with these tools.

## Testing an app

1. `apps` lists the installed apps; `open_app` starts one (a bundle id, or a name) and `look`
   shows the first screen. Ask for the app's deep links and use them: `open_url` with the app's
   own link reaches a screen in one call.
2. Every action (`tap`, `toggle`, `scroll`, ...) answers with what changed (`+` new, `-` gone,
   `~` changed, `No visible change.`), or the whole list when most of the screen changed.
   **Read it before the next step**: check that the action did what you meant, not just that it
   didn't fail. `Settled in N s.` is how long the screen took to stop moving. Read every answer
   whole: never pipe a call through `head`, `grep` or `sed`. The list of what changed is the part
   you need, and cutting it costs a second `look`.
3. Target with one object: `{id}` (accessibility identifier, React Native `testID`) when there
   is one, else `{label}` or `{text}`, else `{ref}` from the latest list. `{x, y}` (points) only
   for things with no element: a canvas, a map. `index` picks among several matches.
4. Actions send no screenshot unless you pass `screenshot: true`; `look` has `labels: true` to
   draw each element's ref on the picture. `look: false` on an action returns only its first line.
5. A screen may still be loading: `wait_for` text, an id or a label (or `gone`), instead of
   guessing a delay with `wait`.
6. Lists: the first line of a list says how much lies off screen (`+41 below`). To reach an
   element there, `scroll` with it as `target` and `to: "target"`: one call brings it on
   screen. `to: "end"` and `to: "start"` go to a list's ends; `down` shows what is below.
7. Text: `type_text` into a target; without one it types into the only field. `replace` clears
   first (the default); `submit` presses return. Switches: `toggle`. `back` goes up one screen.
8. System alerts (notifications, location, tracking) show as `Alert:` in the list. Answer them
   with `alert` the way the test needs, usually as a new user would. In personal mode this works
   for a permission prompt of an app you were given; after the answer the call waits for the app
   to come back.
9. Start a fresh run with `close_app` then `open_app`.
10. When you know the next two or more steps, send them as one `run`: its `steps` are ordinary
    calls, each with an optional `expect` (`{"text": "Saved"}`) that must hold after it; put one on
    the steps that matter. It stops at the first that fails and says which.
    `record` saves what you do as a script that `replay` (or `tapwright replay FILE` in CI) runs
    again. `audit` checks the screen's accessibility; report what it finds. `compare_screen` checks how a
    screen looks against a saved reference (`"expect": {"matches": "<name>"}` in a step); `filmstrip` shows an
    animation as one numbered picture. When something fails, `logs` (the app's own log) and `crash` usually say
    why; `performance` measures launch time, memory, CPU and frame rate.
11. A tap on Buy, Delete, Send, Post, Sign out and the like answers
    `not_allowed: needs_confirmation`: the live view asks the person; repeat the same call once
    they allow it, or ask your user. Never look for another way. `dry_run: true` shows what a touch
    would do without touching.
12. A person may be watching the live view (`viewer` opens it for the user), or driving. Their input
    goes first. Errors say what to do: `not_found` (look, then pick another target), `ambiguous`
    (use a candidate's ref, or `index`), `not_actionable` (the first word says why: `disabled`,
    `off_screen`, `covered`, `under_tab_bar`, `behind_alert`, `no_action`; fix the cause, or pass
    `force` where it can help), `stale` (look again), `busy` (a person is using the phone: wait a
    few seconds, retry), `paused` (ask the user to resume), `locked` (ask the user to unlock the
    phone, or to enter its passcode when the message says the phone is asking for it),
    `not_allowed` (the app isn't one you were given: go back with `open_app`), `not_answering` (the app in front stopped answering: `close_app` it, then `open_app` it),
    `reconnecting` (wait, then look), `timeout` (look at the screen before retrying,
    because the action may have happened), `expect_failed` (a `run` step's check didn't hold: look,
    then report the bug or fix the step).

## Reporting

For each problem: what you did (the steps, short), what you expected, what happened, and
where (screen, element). Mention the screenshot moments that show it. Separate real
bugs from things you couldn't test (blocked by login, by a missing test account, by Face
ID). Note slow screens if they took more than a few seconds.

## Rules

- **It is the user's personal phone.** Don't buy, subscribe, send messages or email,
  post, delete data, or change account, privacy or security settings, unless the user
  asked for exactly that step. Ask when unsure.
- Use test accounts and test data the user gives you. Don't sign in with credentials you
  find on the device.
- Don't read or repeat private content you see (messages, photos, notifications) beyond
  what the test needs.
- You can't pass Face ID, Touch ID or the passcode. If the phone is locked, ask the user
  to unlock it.
- Stay in the app under test. If something takes you elsewhere (a link opens Safari),
  go back with `open_app`.
- **Personal mode is the default.** Add the server with the apps you may use:
  `tapwright mcp --apps com.example.app` (bundle ids or exact names, comma-separated). Tapwright
  then refuses to read or touch any other app (`not_allowed: outside_apps`), and never shows
  another app's screen, except a permission prompt raised by one of the given apps. Without `--apps` nothing runs (`not_allowed: no_apps`). Only on a phone
  kept for testing, start it with `tapwright mcp --dedicated`: then every app can be used.
- The owner stops you by pausing in the live view, or by locking the phone.
