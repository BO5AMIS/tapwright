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
tapwright install --client claude      # or --client codex, --client cursor
```

- Claude Code: it runs `claude mcp add` for you (user scope; `--project DIR` for one
  project). If `claude` is not on the PATH, it prints the exact command.
- Codex: it edits `[mcp_servers.tapwright]` in `~/.codex/config.toml`. Other servers and
  comments stay as they are.
- Cursor: it edits `mcpServers.tapwright` in `~/.cursor/mcp.json` (`.cursor/mcp.json` with
  `--project DIR`).
- Run it again to update the integration; it never adds a second entry.
- Whole-phone access is the default in 1.7.5. No app list or dedicated mode is required. Legacy app/policy options do not restrict calls; see [migration](agent-directed-control.md).

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
- **`tapwright serve`** starts the authenticated local daemon with whole-phone access.
- **`tapwright call`** sends one JSON call to that running daemon without starting another server. Example: `tapwright call look` or `tapwright call tap '{"target":{"id":"save"}}'`. Use a persistent MCP connection for lower per-call overhead.

### Agent-directed control in 1.7.5

App lists, word policies, grants and draftsOnly gates no longer restrict actions. Agents follow the user's goal and existing authorization and clarify consequential work outside it. Pause, authentication, input ownership, dry-run and Apple platform gates remain. `--phone` retains its explicitly configured automatic-unlock behavior and is not required for device access.

See [the control and migration contract](agent-directed-control.md). Install a pinned artifact in an isolated environment for unattended runs. Coordinate an update with the active device owner before stopping or starting its daemon.

### Schedules and the phone

- **One run on the phone at a time.** Scheduled runs and notification checks share a phone lock
  (`~/.tapwright/schedules/.phone.lock`). A run that finds it held waits, then reports `skipped: the phone was in use`.
  If a crash leaves it behind, `tapwright doctor` names it and `tapwright schedule unlock --phone` removes it. A lock
  file that is empty is removed only once it is 2 seconds old (younger, its holder may still be writing it), and a
  stuck `.takeover` beside a lock is cleared under a lock of its own, so an unlock never displaces a run that took the
  lock meanwhile.
- **The 90-second rule.** For a schedule, the phone is in use while a call is running, when an agent called in the
  last 90 seconds, or when you acted in the live view in the last 90 seconds. A run waits up to 10 minutes for that.
  While a scheduled run drives, other agents' actions answer `busy`. The daemon also checks this itself when the run
  starts: if an agent or you got to the phone after the run's last look, the run waits and asks again. A notification
  trigger's check is decided again the moment it gets the phone, so an agent's call that came in meanwhile makes it
  wait too.
- **Notification Center is closed, or the answer says it may not be.** If closing it fails, the app that was in front
  is opened again anyway; if Notification Center can't be shown closed, the call answers `internal` and says to look
  at the screen first, instead of answering as if nothing were wrong. Tapline's `/otp` answers the same `internal`
  error (HTTP 500), with no code.
- **A run's errors are redacted.** A scheduled run's audit line and report go through its vault's redactor before
  anything is cut or written, so a vault value in a file name or an error never reaches either. That holds when the
  schedule file itself is wrong too: the vault it names is read first. When that vault can't be read, the error only
  says the schedule isn't valid, without its details.
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
| the live view says "missing or wrong token" or "This link has no valid key" | run `tapwright view` on the attached computer; it opens the authenticated page in its browser without printing the key. Opening just `localhost:8417`, or a masked copy of its URL in another browser, does not authenticate it |
| port 8417 in use | the daemon takes a free port by itself; `tapwright view` finds it |
| "another tapwright serve is starting or stuck" | wait a minute and retry; if it stays, see `~/.tapwright/serve.log`, end the stuck process, then `tapwright serve` |

### A system alert blocks a workflow

Use `alert {"action":"text"}` to read it. Answer with `accept`/`dismiss`, optionally `name` for an exact visible button, or target it from a fresh look. `cancel` defaults to Cancel and supports localized names. No extra app scope or policy exception is required. Use dry_run to inspect without input. A genuine XCTest passcode/trust gate remains an Apple requirement.

## Undo

Delete *WebDriverAgentRunner-Runner* on the phone, turn Developer Mode off if you like,
remove `~/.tapwright`. In the developer portal, revoke the "tapwright" development
certificate and delete the profile and the `<prefix>.*` App ID if you created them.
