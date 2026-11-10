# Quickstart

Let an AI agent test any app on your iPhone, from Windows, Linux or macOS, with no Mac. The
phone side needs an Apple developer account once, and about ten minutes. Detail and every
option: [setup.md](setup.md).

## What you need

- A computer with Python 3.10 or later. On Windows also **Apple Devices** from the Microsoft
  Store, which provides the USB connection.
- An iPhone on a USB cable, and its passcode.
- A paid Apple Developer Program membership, for an App Store Connect API key (Admin role).
  Apple only runs UI automation from a development-signed test runner.
- An AI agent client that speaks MCP, such as Claude Code.

## On the computer

```sh
pip install git+https://github.com/<owner>/tapwright
tapwright doctor
```

- `tapwright doctor` checks Python, the USB connection, the signing files and the phone. It
  says how to fix anything that's missing. Fix that before you go on.
- These steps haven't been timed from a clean computer yet. On a computer that is set up,
  `tapwright doctor` takes a few seconds.

## On the phone

Once per phone, with the phone plugged in and unlocked. See [setup.md](setup.md) for each
step in full.

1. Create the API key in App Store Connect and note its Key ID, Issuer ID and the `.p8`
   file. Choose a bundle prefix that is yours, such as `com.yourname.tapwright`.
2. Run setup:
   ```sh
   tapwright setup --asc-key-id KEYID --asc-issuer-id ISSUER --asc-key-file AuthKey_KEYID.p8 --bundle-prefix com.yourname.tapwright
   ```
3. The phone asks **Trust This Computer?** Tap Trust and enter the passcode.
4. If setup says Developer Mode is off: Settings > Privacy & Security > **Developer Mode**,
   turn it on, restart, confirm, and run setup again.
5. Check it: `tapwright smoke --app <bundle id of one of your apps>`. The first start mounts
   Apple's developer disk image and can take up to a minute. Later starts take about 2 s.
6. The phone may show **Enter iPhone Passcode for "XCTest"** (Enable UI Automation). Enter
   it. To stop the phone asking again, turn on Settings > Developer > **Enable UI
   Automation**: on a test device, do this now.
7. Give Tapwright to your agent, naming the apps it may use (see "Personal phone or
   dedicated device"):
   ```sh
   claude mcp add tapwright -- tapwright mcp --apps com.example.app
   ```

## First test

Unlock the phone. Ask your agent: "Open the app, go to the login screen and tap Log in." It
makes three calls.

**1. `open_app`** with `{app: "com.example.app"}` starts the app and answers with its first
screen:

```
Opened Example app (com.example.app).
Screen 430x932 pt @3x · 3 elements
App: Example app (com.example.app)
e1 text "Welcome back" @24,120 200x32
e2 input "Email" id=email @24,200 354x44 [tap, type]
e3 button "Log in" id=login-button @24,610 354x48 [tap]
Settled in 0.84 s.
```

**2. `look`** reads the screen again as a list, with a screenshot. Use it whenever you
aren't sure what is on screen.

**3. `tap`** with `{id: "login-button"}` answers with what it did and what changed:

```
Tapped e3 button "Log in" id=login-button @24,610 354x48.
Same screen. Changes:
+ e4 text "Check your email and password" @24,670 354x20
Settled in 0.31 s.
```

A good answer names the element it tapped and says what changed. When a touch couldn't work,
the answer says why: `covered`, `off_screen`, `disabled`, `behind_alert`. Give the controls
you test an accessibility identifier (React Native: `testID`): the agent targets it as `id`,
which keeps tests stable.

## Watching and stopping

- `tapwright view`, or the agent's `viewer` tool, gives a link to open in a browser on your
  computer: the phone's screen, live, with the agent's touches marked. The viewer shows who is
  driving, you or the agent. You can tap, drag and type there too, and your input goes first.
- For a smooth picture, start the service with `tapwright serve --video` (same Wi-Fi; see
  [setup.md](setup.md#the-live-view-as-video)): the phone then mirrors its screen as video, 60
  frames a second.
- **Pause agent** in the viewer stops the agent until you press Resume.
- On the phone, **lock it**. Every action then answers `locked` until you unlock it.
- `tapwright serve --stop` on the computer ends the session.

## Personal phone or dedicated device

**Your own phone.** Start Tapwright with the apps the agent may use:
`tapwright mcp --apps com.example.app` (bundle ids or exact names, comma-separated). Tapwright
then refuses to read or touch any other app, and never shows another app's screen to the
agent. If something takes the phone elsewhere, the agent is told which app is in front and can
only go back. Without `--apps` the agent can do nothing. iOS's own prompts (a password
prompt, a call) belong to the Home screen: the agent can't see or answer them, so you may have
to dismiss one yourself.

**A dedicated test device.** Start it with `tapwright mcp --dedicated`: the agent may use
every app, the Home screen and system prompts. Use this only on a phone kept for testing,
with no personal accounts on it. This is the setup Tapwright is measured on, and the one we
recommend.

## If something doesn't work

Run `tapwright doctor` first. Then the three most common failures:

- **The agent gets `locked`.** Unlock the phone. Tapwright can't pass Face ID or the passcode.
  If the message says the phone is asking for its passcode to allow UI automation, the phone
  shows **Enter iPhone Passcode for "XCTest"**: enter it, and turn on Settings > Developer >
  **Enable UI Automation** so it doesn't ask again.
- **The agent gets `not_answering`.** The app in front has frozen. The agent can close it with
  `close_app` and open it again.
- **The agent gets `unsupported: no_apps` or `outside_apps`.** It was started without an app
  list, or another app is in front. Add `--apps` (or `--dedicated`), or let it go back with
  `open_app`.

More in the troubleshooting table in [setup.md](setup.md#troubleshooting).
