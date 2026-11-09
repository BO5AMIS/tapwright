# Tapwright: guide for agents

You can see and operate the user's real iPhone through Tapwright's MCP tools, to test apps:
the user's own builds (TestFlight, development) or any installed app. This guide covers
setting Tapwright up for a user, then testing well and safely.

## Setting it up for a user

Do the computer side yourself; ask the user only for what needs their hands or their
Apple account. Follow [docs/setup.md](docs/setup.md).

1. `pip install git+https://github.com/<owner>/tapwright`, then `tapwright doctor`. Fix
   what it reports on the computer (Windows: Apple Devices from the Microsoft Store).
2. Ask the user to plug in and unlock the iPhone.
3. Signing: ask for an App Store Connect API key (Key ID, Issuer ID, the `.p8` file's path,
   Admin role) and a bundle prefix they own (`com.theirname.tapwright`), or for their own
   `.p12` and profile. Never print or store the key's contents anywhere else.
4. Run `tapwright setup …`. Relay what the phone shows: **Trust This Computer?**, and if
   setup says so, turning on Developer Mode (Settings > Privacy & Security > Developer
   Mode, restart, confirm), then run setup again.
5. `tapwright smoke --app <one of their bundle ids>` proves the chain. Then add the MCP
   server to your host (Claude Code: `claude mcp add tapwright -- tapwright mcp`).

## Testing an app

1. `apps` lists the installed apps; `open_app` starts one (a bundle id, or a name) and `look`
   shows the first screen.
2. Every action (`tap`, `toggle`, `scroll`, ...) answers with what changed (`+` new, `-` gone,
   `~` changed, `No visible change.`), or the whole list when most of the screen changed.
   **Read it before the next step**: check that the action did what you meant, not just that it
   didn't fail. `Settled in N s.` is how long the screen took to stop moving.
3. Target with one object: `{id}` (accessibility identifier, React Native `testID`) when there
   is one, else `{label}` or `{text}`, else `{ref}` from the latest list. `{x, y}` (points) only
   for things with no element: a canvas, a map. `index` picks among several matches.
4. Actions send no screenshot unless you pass `screenshot: true`; `look` has `labels: true` to
   draw each element's ref on the picture. `look: false` on an action returns only its first line.
5. A screen may still be loading: `wait_for` text, an id or a label (or `gone`), instead of
   guessing a delay with `wait`.
6. Lists: `scroll` until the element you need appears, or `scroll` with `to: "end"`. `down`
   shows what is below.
7. Text: `type_text` into a target; without one it types into the only field. `replace` clears
   first (the default); `submit` presses return. Switches: `toggle`. `back` goes up one screen.
8. System alerts (notifications, location, tracking) show as `Alert:` in the list. Answer them
   with `alert` the way the test needs, usually as a new user would.
9. Start a fresh run with `close_app` then `open_app`.
10. A person may be watching the live view (`viewer` gives the link), or driving. Their input
    goes first. Errors say what to do: `not_found` (look, then pick another target), `ambiguous`
    (use a candidate's ref, or `index`), `not_actionable` (the first word says why: `disabled`,
    `off_screen`, `covered`, `under_tab_bar`, `behind_alert`, `no_action`; fix the cause, or pass
    `force` where it can help), `stale` (look again), `busy` (a person is using the phone: wait a
    few seconds, retry), `paused` (ask the user to resume), `locked` (ask the user to unlock the
    phone), `reconnecting` (wait, then look), `timeout` (look at the screen before retrying,
    because the action may have happened).

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
