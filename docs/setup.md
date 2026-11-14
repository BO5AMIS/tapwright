# Setup

Once per phone, with the phone on USB. About ten minutes.

## 1. The computer

- Python 3.10 or newer, then `pip install tapwright`.
- Windows: **Apple Devices** from the Microsoft Store (or iTunes). It provides the USB
  connection to the phone.
- Linux: `usbmuxd` (`sudo apt install usbmuxd`).
- macOS: nothing else.

For QA or any unattended run, install a pinned release in its own virtual environment
(`pip install tapwright==<version>`, or the wheel), and never run from a development checkout:
a half-merged working tree breaks every call. `tapwright doctor` warns when it finds this.

`tapwright doctor` checks these at any time.

## 2. A signing identity

Apple runs UI automation only from a development-signed test runner whose profile lists
your phone. Pick one way.

**A. App Store Connect API key (recommended).** Needs a paid Apple Developer Program
membership.

1. In [App Store Connect](https://appstoreconnect.apple.com) > Users and Access >
   Integrations > App Store Connect API, create a key with the **Admin** role (creating
   certificates and profiles needs it).
2. Note the **Key ID** and **Issuer ID**, and download `AuthKey_<KEYID>.p8` (only once
   possible; keep it safe).
3. Choose a bundle prefix in reverse-DNS form that is yours, such as
   `com.yourname.tapwright`. Setup creates the wildcard App ID `com.yourname.tapwright.*`,
   under which WebDriverAgent gets two bundle IDs.

Setup then registers the phone, creates an Apple Development certificate (the private
key is generated on your computer and stays in `~/.tapwright/signing`), and creates a
development profile. Running setup again reuses the certificate and makes a new profile.

**B. Your own files.** An Apple Development certificate exported as `.p12` (with its
private key), and a development profile (`.mobileprovision`) for a **wildcard** App ID
(for example `com.yourname.tapwright.*`) that includes the phone:

```sh
tapwright setup --p12 dev.p12 --p12-password PASSWORD --profile tapwright.mobileprovision
```

## 3. The phone

1. Plug it in, unlock it.
2. Run setup (with A or B). The phone asks **Trust This Computer?**: tap Trust, enter
   the passcode.
3. If setup reports Developer Mode off: Settings > Privacy & Security > **Developer
   Mode**, turn it on, restart, then confirm **Turn On** after unlocking. Run setup again.

Setup ends with WebDriverAgent installed (it appears as *WebDriverAgentRunner-Runner*)
and the phone paired for network use.

Run setup again any time. It skips what is done (WebDriverAgent installed and signed for
more than 14 more days, network pairing saved) and ends with the next two commands.
`--force` does every step again.

## 4. Check

```sh
tapwright doctor
tapwright smoke --app <bundle id of one of your apps>
```

Then connect your agent. One command registers the MCP server and installs the skill:

```sh
tapwright install --client claude --apps <bundle id>      # or --client codex, --client cursor
```

- Claude Code: it runs `claude mcp add` for you (user scope; `--project DIR` for one
  project). If `claude` is not on the PATH, it prints the exact command.
- Codex: it edits `[mcp_servers.tapwright]` in `~/.codex/config.toml`. Other servers and
  comments stay as they are.
- Cursor: it edits `mcpServers.tapwright` in `~/.cursor/mcp.json` (`.cursor/mcp.json` with
  `--project DIR`).
- Run it again to change the apps: it updates the entry, never adds a second one.
- It needs `--apps` or `--dedicated`, like `tapwright mcp`. Without either, the agent can do
  nothing.

### Driving fast: the options

`install` also writes the driver agent (`phone-driver`: `.claude/agents/phone-driver.md` for Claude Code,
`.codex/agents/phone-driver.toml` for Codex) on that host's fastest small model; `--driver-model NAME` changes the
model. No model runs inside Tapwright and no API key is asked for. The options below go on `tapwright mcp`
(`tapwright install` doesn't carry them: add them to the registered command), and `tapwright serve` takes the same
ones as the daemon's own settings.

- **`--accounts FILE`**: test accounts, kept on your computer. A JSON file:

  ```json
  {"accounts": {"tester2": {"username": "tester2@example.com", "password": "...", "apps": ["com.example.app"]}}}
  ```

  An agent says `account: "tester2"` in `drive`, and the tool types the values: a password only into a secure
  field, a username only into a text field. They are never in an answer, the action log, a map, a script or a
  report. The file may not sit inside a git work tree unless git ignores it. `tapwright doctor --accounts FILE`
  checks it. An entry named `phone` with a `passcode` (only that name) unlocks a locked phone on its lock screen, and
  only where nobody is meant to be at the phone: at the start of a scheduled run, and with `tapwright serve --phone
  --accounts FILE` at the start of a new call. The passcode comes only from the file `tapwright serve --accounts`
  names. A lock in the middle of a run stops it (`locked`): locking the phone is how you stop the agent. At most 2
  attempts an hour and 3 a day, kept across restarts in `~/.tapwright/unlock-state.json`; after the phone refuses the
  passcode nothing more is typed until you check the file and run `tapwright unlock-reset`.
  **Experimental in 1.7:** on the test iPhone (Face ID, iOS 26) the unlock didn't yet find the passcode keypad. It
  answered `locked` and typed nothing. iOS also won't open the USB connection to a phone that is locked when the daemon
  starts. Plan unattended runs with the phone unlocked for now.
- **`--maps DIR`**: where each app's map is kept (`<DIR>/<bundle id>/map.json`, and its `routes/`). The default is
  `~/.tapwright/maps`. Point it at a folder in your project and a team, CI and Tapline share what was learned.
- **`--brief`**: answer every action in brief (the first line, at most 12 lines of changes, then `...N more:
  look`) unless the call says `brief: false`. `look` always answers whole.
- **`--no-dialog`**: never ask the host's dialog (MCP elicitation) for a person's OK or a moment that needs a
  person; only the live view answers. For a host that passes such a question to the model instead of showing it.
  Without it, a host that can show the dialog does, and the first answer, in chat or in the live view, wins.
- **`tapwright serve --apps A,B`** (or `--dedicated`): the ceiling, the widest scope any call may ask for, from
  an MCP server or from `tapwright call`. A call may name these apps or fewer, never more, and its own `--policy`
  may add words that need an OK or are blocked, never remove one (`serve --policy FILE` sets the daemon's own).
  Without `--apps` or `--dedicated` the daemon refuses every call (no scope, nothing allowed), and adding `--apps` to
  each call doesn't fix that: restart it with a scope. The flags apply when the daemon starts: a daemon that is
  already running keeps its own, so `tapwright serve --stop` first.
- **`tapwright call TOOL ['JSON']`**: one tool without an MCP connection, for a host that can't keep one. It talks to
  the running daemon (it never starts one: `tapwright serve` does) and loads only Python's standard library, so it
  starts in a fraction of a second. For example `tapwright call look --apps com.example.app` and
  `tapwright call tap '{"target": {"id": "save"}}' --apps com.example.app`; `--apps` (or `$TAPWRIGHT_APPS`) is the
  scope, and without it the call is refused, as for an MCP server. Also `--dedicated`, `--policy`, `--maps`,
  `--client NAME` (keeps one run's report and journey together: pass one), `--json` and `--timeout`. It exits 0 with
  the answer, 2 for a refusal (`Error <code>: <message>` on stderr) or a mistake in the command, and 3 when the
  daemon can't be reached. Loading the MCP server once, when the session starts, is faster still.

### Safety rules of 1.7 worth knowing

- **`~/.tapwright/serve.json` is trusted like you.** It holds the daemon's key. Any process of your user that can
  read it can answer the approval dialog (including the offer to make a grant, so it can create a standing grant),
  fetch one-time codes and ask the daemon to unlock the phone. An agent that runs shell commands as you can read it.
  Keep the file private, and run agents you don't trust where they can't read your home folder.
- **Locking the phone stops a run.** A lock in the middle of a `run`, `drive`, `replay`, route or journey ends it with
  `locked`; the passcode is never typed there. The tool unlocks a locked phone only at the start of a scheduled run,
  and with `tapwright serve --phone --accounts FILE` at the start of a new call.
- **Unlock limits.** At most 2 passcode attempts an hour and 3 a day, counted across restarts in
  `~/.tapwright/unlock-state.json`. Once the phone refuses the passcode, nothing more is typed until you check the
  vault file and run `tapwright unlock-reset` (it clears the refusal; the counts stay).
- **One-time codes only into a code field.** `${otp.<app>}` is typed only into a one-line field whose id, label or
  placeholder says code, OTP, verification or PIN. Any other field is refused (`not_allowed`) and no code is read. A
  draft that holds a code says so. Tapline's `/otp` gets a code only while the tool's own read shows the app in front
  with such a field, and each code goes out once, also across a restart of the daemon: a handed-over code leaves a
  mark (a keyed digest, never the code) in `~/.tapwright/otp-used` for 10 minutes.
- **Drafts, whatever the policy file says.** In a route with `"draftsOnly": true`, or under a draftsOnly grant, a
  touch on Send, Post, Reply, Comment and the like stops at a draft even when your policy file has no such word, or
  with `--no-policy`. A grant without draftsOnly doesn't lift a route's draftsOnly. `type_text` with `submit: true` is
  refused there: type without it, then tap the send button. Your OK is for the draft you saw: if the text, or any
  field on the screen, changed before the send, nothing is sent and you are asked again.
- **Deleting an account always asks you.** No grant covers a Delete or Confirm in an alert or on a screen that says it
  deletes the account ("Delete your account?"), or in an alert about a payment, and no grant is offered for it.
- **A full swipe across a list row needs your OK.** A horizontal swipe that starts on a row and goes further than 40%
  of its width runs the row's first action (often Delete or Archive), so it asks you, and no grant covers it. In a
  draftsOnly route or a scheduled run it is refused. A short swipe only shows the row's buttons; tap the one you want.
- **`--revoke-all` always works.** If the policy file's edit lock is held, it writes `<policy file>.revoked`, which
  revokes every grant at once; the next change to the file writes the revocation in and removes the mark. It also
  stops a running batch or drive that began before it, whatever grants it began with.
- **A vault in the macOS keychain can't be named `…#<number>`.** The keychain keeps those names for the pieces of a
  large vault, so `vault protect` refuses such a file name; rename it first.
- **`--apps` names one app each.** A name in `serve --apps` or in a call must be the name of exactly one installed
  app (ignoring case), or a bundle id. A name two apps share is refused: use the bundle id.

### Schedules and the phone

- **One run on the phone at a time.** Scheduled runs and notification checks share a phone lock
  (`~/.tapwright/schedules/.phone.lock`). A run that finds it held waits, then reports `skipped: the phone was in use`.
  If a crash leaves it behind, `tapwright doctor` names it and `tapwright schedule unlock --phone` removes it.
- **The 90-second rule.** For a schedule, the phone is in use while a call is running, when an agent called in the
  last 90 seconds, or when you acted in the live view in the last 90 seconds. A run waits up to 10 minutes for that.
  While a scheduled run drives, other agents' actions answer `busy`. The daemon also checks this itself when the run
  starts: if an agent or you got to the phone after the run's last look, the run waits and asks again.
- **A run opens its app.** A scheduled run opens its route's app first, so it doesn't matter which app (or the home
  screen) is in front when it falls due.
- **A locked phone.** `run-schedule` starts `tapwright serve` with the schedule's own `--accounts` when no daemon is
  running, so the unlock at a run's start can use that file's passcode. A daemon that is already running keeps the
  `--accounts` it was started with: if that's another file, the run is skipped as locked.
- **A skipped trigger isn't retried.** A file or notification trigger marks what it saw before the run starts. If the
  run is then skipped (the previous run still going, the phone in use or locked), that file or notification doesn't
  start a run later. The skip is in the audit log and the run's report.
- **Notification checks touch the phone.** A notification trigger pulls Notification Center down over the screen for
  about a second every 2 minutes while the PC is awake. It waits while the tool or the live view uses the phone, but
  it can't see you holding the phone. `--on-notification` takes the app's bundle id or its exact name.
- **Missed runs.** A run that falls due while the PC is off, or asleep with wake timers off, doesn't run later and
  leaves no audit line. Runs happen only while you're logged on to Windows. `tapwright doctor --unattended` says both.

### Keeping it working

The development profile and certificate last a year. Then WebDriverAgent stops starting.

- `tapwright doctor` warns, with the date, 30 days before the profile or certificate runs
  out. The agent's first answer of a session carries the same warning.
- `tapwright doctor --fix` (phone on USB, `tapwright serve` stopped) repairs, step by step,
  and says what it did:
  1. The developer disk image is not mounted: it mounts it.
  2. WebDriverAgent is missing from the phone, or its signature ends within 14 days or has
     ended: it signs it again with the profile and certificate you already have, and
     installs it.
  3. The network pairing port is stale: it looks for the phone on the Wi-Fi and saves the
     current port (as `tapwright probe` does).
- `--fix` never makes a certificate or a profile. If the profile itself has run out, or is
  about to, it says so: run `tapwright setup` again with your App Store Connect key (it
  makes the new profile and installs WebDriverAgent again).

The first start of a session mounts Apple's developer disk image on the phone (downloaded
once, needs internet) and starts WebDriverAgent; that takes up to a minute. Later starts
take seconds.

**The passcode screen.** Before it lets a UI test start, iOS may show **Enter iPhone Passcode
for "XCTest"** with the line *Enable UI Automation*. It does so on a first run, and again
every few hours (on our phone it came back two and a half hours after the passcode was
entered). Nothing starts until
a person enters the passcode; Tapwright can't, and says so: agents get `locked` with "the
phone is asking for its passcode to allow UI automation", and the live view shows the same.
The start goes on by itself once the passcode is in.

To stop the phone asking, turn on Settings > Developer > **Enable UI Automation**. Entering
the passcode only lasts a few hours; the setting lasts. Do this on every dedicated test
device, or an unattended run stops at that screen.

## 5. The live view

```sh
tapwright view
```

opens the phone's screen in your browser, live, in a drawn iPhone, and lets you drive it. Each
step is captioned under the phone; the bar has Pause agent, Steps and More (Home, volume, the
picture quality, Details). It starts
`tapwright serve` (the background process that owns the phone) if it isn't running; agent
tools start it too. It stops by itself after 30 minutes without viewers or requests
(`tapwright serve --keep-alive` keeps it running), or with `tapwright serve --stop`. Its log
is `~/.tapwright/serve.log`.

### The live view as video

By default the picture is a stream of JPEG frames: about 26 a second, at a cost to the phone
that grows with the frame rate. With `--video` the phone mirrors its own screen to the
computer over AirPlay, and the live view plays that as H.264: 60 frames a second at about
20 KB/s, with nothing installed on the phone.

```sh
pip install "tapwright[video]"
tapwright serve --video
```

- The phone and the computer must be on the same Wi-Fi. USB still carries the control.
- It needs [UxPlay](https://github.com/FDH2/UxPlay), an open-source AirPlay receiver, on the
  computer: `uxplay` on the PATH, or the path in `TAPWRIGHT_UXPLAY`. On Windows it is built
  with MSYS2 (`pacman -S` its dependencies, then its own build steps); Tapwright finds the
  build in `C:\msys64\ucrt64\bin`. The firewall must let the phone reach it on this network.
- Tapwright starts Screen Mirroring itself, the way you would: it opens Control Center, taps
  Screen Mirroring and picks "Tapwright", then returns to the app that was in front. This
  takes about 13 seconds after the phone connects. Mirroring ends when `tapwright serve` stops.
- If anything is missing, the log says what, and the live view stays on the JPEG stream.
- While the screen is still, the phone sends nothing. Under constant animation mirroring
  costs the phone about two thirds of one CPU core.

## Troubleshooting

| Symptom | Fix |
|---|---|
| no iPhone on USB | unlock the phone; another cable or port; Windows: open Apple Devices once |
| "the profile doesn't include this iPhone" | run setup with the API key (it makes a new profile), or make one that lists the phone |
| WebDriverAgent doesn't start | phone unlocked and screen on; Developer Mode on; `tapwright doctor --fix` (or run setup again) to reinstall |
| the phone shows "Enter iPhone Passcode for XCTest" | enter it; then Settings > Developer > Enable UI Automation, so it doesn't ask again |
| works on USB, not on Wi-Fi | same network, no client isolation; run `tapwright probe` with the phone on the Wi-Fi |
| actions do nothing on a screen | look again: the screen may have changed; system alerts need `alert` |
| "tapwright serve is running and owns the phone" | `smoke` and `probe` need the phone to themselves: `tapwright serve --stop` |
| the live view says "missing or wrong token" | open the link `tapwright view` prints; `tapwright serve --new-token` makes a new one |
| port 8417 in use | the daemon takes a free port by itself; `tapwright view` finds it |
| "another tapwright serve is starting or stuck" | wait a minute and retry; if it stays, see `~/.tapwright/serve.log`, end the stuck process, then `tapwright serve` |

## Undo

Delete *WebDriverAgentRunner-Runner* on the phone, turn Developer Mode off if you like,
remove `~/.tapwright`. In the developer portal, revoke the "tapwright" development
certificate and delete the profile and the `<prefix>.*` App ID if you created them.
