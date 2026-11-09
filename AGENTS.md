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

1. `list_apps` to find the bundle id; `launch_app` (it returns the first screen).
2. Every action returns the screen after it. **Read it before the next step**: check
   that the action did what you meant, not just that it didn't fail.
3. Target elements by `id` (accessibility identifier, React Native `testID`) when there
   is one, else by `label`, else by `ref` from the latest look. Use `x`/`y` only for
   things with no element (a canvas, a map); positions are in points.
4. Screens load and animate. If the screen looks mid-transition or a spinner shows,
   `wait` (0.5-3 s) instead of acting on it.
5. Lists: `scroll` until the element you need appears. `down` shows what is below.
6. Text: `type_text` with the field as target. `replace: true` clears what's there;
   `submit: true` presses return.
7. System alerts (notifications, location, tracking) appear in `look`. Answer them with
   `alert` the way the test needs, usually as a new user would.
8. Start a fresh run with `terminate_app` then `launch_app`.

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
  go back with `launch_app`.
