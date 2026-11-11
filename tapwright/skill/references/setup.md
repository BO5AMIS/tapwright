# Setting Tapwright up for a user

Do the computer side yourself. Ask the user only for what needs their hands or their Apple
account. The full guide is `docs/setup.md` in the Tapwright repository; this is the order.

## What is needed

- An iPhone on iOS 17 or later, and a USB cable for the first setup.
- A computer with Python 3.10 or later. On Windows: the **Apple Devices** app from the
  Microsoft Store. On Linux: `usbmuxd`.
- An Apple Developer Program membership, for an App Store Connect API key (Key ID, Issuer
  ID, the `.p8` file). Or the user's own Apple Development certificate (`.p12`) and a
  development profile that includes the phone.

Never print the key's contents, and never copy the key file anywhere else.

## Steps

1. Install and check the computer:
   ```sh
   pip install tapwright
   tapwright doctor
   ```
   Fix what `doctor` reports on the computer.
2. Ask the user to plug the iPhone in and unlock it.
3. Run setup with the user's key and a bundle prefix they own:
   ```sh
   tapwright setup --asc-key-id KEYID --asc-issuer-id ISSUER --asc-key-file AuthKey_KEYID.p8 --bundle-prefix com.theirname.tapwright
   ```
   Relay what the phone shows:
   - **Trust This Computer?** The user taps Trust and enters the passcode.
   - If setup says Developer Mode is off: Settings > Privacy & Security > Developer Mode,
     turn it on, restart, confirm. Then run setup again.
4. Prove the chain with one of the user's apps:
   ```sh
   tapwright smoke --app com.example.app
   ```
   If the phone shows **Enter iPhone Passcode for "XCTest"** (Enable UI Automation), the user
   enters it. Suggest Settings > Developer > **Enable UI Automation**, which stops the phone
   asking again. On a phone kept for testing this should always be on.
5. Connect the agent, naming the apps the user wants tested. One command registers the MCP
   server and installs this skill:
   ```sh
   tapwright install --client claude --apps com.example.app   # or --client codex, --client cursor
   ```
   Run it again to change the apps: it updates the entry. Setup run a second time skips
   what is done and ends with this step.

## Keeping it working

The signing lasts a year. `tapwright doctor` warns 30 days before it ends, and the first
answer of a session carries the same warning: tell the user. `tapwright doctor --fix`
(phone on USB) mounts the developer disk image, installs or re-signs WebDriverAgent with the
stored profile, and refreshes the network pairing. It never makes a new profile: when it says
so, ask the user for the App Store Connect key and run `tapwright setup` again.

## The MCP server, by hand, per host

`tapwright install` does the following for you.

The server is a stdio command:

```sh
tapwright mcp --apps com.example.app
```

- `--apps` takes bundle ids or exact names, comma-separated. Without it every call is
  refused (`not_allowed: no_apps`).
- `--dedicated` in its place lets the agent use every app. Only for a phone kept for
  testing, with no personal accounts on it. Never choose it for the user: ask.
- `--screenshots DIR` saves pictures as files and answers with their path, for hosts and
  agents that shouldn't receive images.

**Claude Code**

```sh
claude mcp add tapwright -- tapwright mcp --apps com.example.app
```

**Codex**, in `~/.codex/config.toml`:

```toml
[mcp_servers.tapwright]
command = "tapwright"
args = ["mcp", "--apps", "com.example.app"]
```

Codex reads this file when it starts: start a new session afterwards.

**Any other MCP client:** register the stdio command above under the name `tapwright`.

## This skill, per host

```sh
tapwright skill install                # for this user: ~/.claude/skills and ~/.agents/skills
tapwright skill install --project .    # for one project: .claude/skills and .agents/skills
```

Claude Code reads `.claude/skills`; Codex and other agents read `.agents/skills`.

## The live view

`tapwright view` opens the phone's screen in the browser. The user can watch, take over,
and pause you. `tapwright serve --video` makes the picture smooth (same Wi-Fi; see
`docs/setup.md`).

## When something doesn't work

Run `tapwright doctor` first.

| Symptom | Fix |
|---|---|
| no iPhone found | unlock it; another cable or port; on Windows open Apple Devices once |
| every call answers `locked` | the phone is locked, or shows a passcode screen: the user must act |
| every call answers `not_allowed: no_apps` | the server was added without `--apps` |
| `not_allowed: outside_apps` | another app is in front: `open_app` the app under test |
| `not_answering` | the app in front froze: `close_app`, then `open_app` |
| WebDriverAgent doesn't start | phone unlocked, Developer Mode on; run `tapwright doctor --fix` (or `tapwright setup` again) |
