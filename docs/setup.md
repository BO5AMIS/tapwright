# Setup

Once per phone, with the phone on USB. About ten minutes.

## 1. The computer

- Python 3.10 or newer, then `pip install tapwright`. It installs pymobiledevice3 11.63 or later, which 2.0 needs.
- An iPhone on iOS 17 or later. Touches from the computer (taps, swipes, scrolls, `pinch`, `press_button lock`)
  need iOS 18 or later (proven on iOS 26). On iOS 17, and on iOS 27 until its display stream is supported, touches go
  through the runner as in 1.10.0, and `pinch` and `press_button lock` answer `unsupported`.
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
  checks it. An entry named `phone` with a `passcode` (only that name) unlocks a locked phone on its lock screen: by
  itself at the start of a scheduled run and, with `tapwright serve --phone --accounts FILE`, at the start of a new
  call; and in every mode when an agent calls `unlock` (1.7.8). The passcode comes only from the file `tapwright serve --accounts`
  names. A lock in the middle of a run stops it (`locked`); the agent may then call `unlock`. At most 2
  failed attempts an hour and 3 a day (an unlock that works isn't limited, from 1.10.0), kept across restarts in `~/.tapwright/unlock-state.json`; after the phone refuses the
  passcode nothing more is typed until you check the file and run `tapwright unlock-reset`.
  To save the passcode, add the `phone` entry to the same file, next to the accounts (`tapwright vault protect FILE`
  encrypts the file):

  ```json
  {"version": 1, "accounts": {"phone": {"passcode": "<the phone's passcode>"},
                               "tester2": {"username": "tester2@example.com", "password": "..."}}}
  ```

  Start Tapwright with that file (`tapwright mcp --accounts FILE`, or `tapwright serve --accounts FILE`). An agent
  then calls `unlock` when the phone is locked, in every mode (1.7.8); without the entry it answers `locked` and
  asks you to unlock the phone. With a passcode saved, locking the phone no longer stops an agent: Pause in the
  live view does.
  **Experimental (1.7, and `unlock` in 1.7.8 not yet tried there):** on the test iPhone (Face ID, iOS 26) the unlock didn't yet find the passcode keypad. It
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

### Switches (Tapwright 2.0)

Environment variables, read when `tapwright serve` starts (on Windows, `setx NAME VALUE`, then `tapwright serve --stop`
so the next call starts a new daemon). None is needed for normal use.

| Variable | Effect |
|---|---|
| `TAPWRIGHT_HANDS=runner` | every touch goes through the runner, as in 1.10.0 (default `auto`: from the computer where it can) |
| `TAPWRIGHT_HANDS_LANDSCAPE=0` | landscape touches go through the runner |
| `TAPWRIGHT_HUMAN=0` | swipes are straight lines, not finger-like |
| `TAPWRIGHT_DIRECT=0` | USB requests to the runner go through the tunnel, not usbmuxd |
| `TAPWRIGHT_TREE_BUDGET` | milliseconds a screen read may take (default 3000) |
| `TAPWRIGHT_AX_TIMEOUT` | seconds any other accessibility read may take (default 3) |
| `TAPWRIGHT_TREE_PATH=direct` or `xctest` | force the runner's way of reading (for measuring) |
| `TAPWRIGHT_TREE_PATH=lean` | every screen read is the runner's fast direct read, as in 2.0.0: quicker looks, but a view another process draws inside an app (an in-app browser sheet) lists no elements |
| `TAPWRIGHT_RUNNER_OCR=0` | read the picture's text on this computer only (RapidOCR), never on the phone |
| `TAPWRIGHT_OCR_LANGUAGES` | languages the phone reads in the picture, for example `en-US,ar-SA` |
| `TAPWRIGHT_TRACE=1` | log the time of each touch and each request to the phone |

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
- **Updating the runner.** Tapwright 2.0 uses runner build `tw-16.15.3.15` (route version 15: reads that keep to their
  time, the app in front without reading it, JPEG screenshots, on-phone text reading). An older runner keeps working,
  as in 1.10.0, without those. To install the new one on an existing install, plug the phone in, stop `tapwright serve`
  (`tapwright serve --stop`), and run `tapwright doctor --fix`: it sees that the phone has another build, signs the new
  runner with the profile and certificate you already have, and installs it (about 20 s). `tapwright setup --force`
  does the same and every other step again.
- A new Tapwright may need a newer runner for a feature: `media add` with videos (mp4, mov, m4v) needs the runner's
  route version 7 (1.7.9), and an older one answers `unsupported` naming `tapwright setup --force`, which installs the
  current runner (pictures still work meanwhile). The video is copied into the runner's own container over USB or
  Wi-Fi first, so a big clip takes a while.

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
tools start it too. With no phone connected it stops by itself after 30 minutes without viewers
or requests; while a phone is connected it keeps running (`tapwright serve --keep-alive` keeps it
running in any case), until `tapwright serve --stop`. Its log is `~/.tapwright/serve.log`.

### Plug and forget: always on

iOS asks for the passcode ("Enter iPhone Passcode for XCTest") when Tapwright starts its test
runner on the phone and the last time you entered it is about 8 hours ago or more. A runner that
keeps running is never asked. So keep the phone plugged in and let Tapwright start once:

```sh
tapwright service install --accounts ~/tapwright-vault.json
```

creates a Windows logon task (hidden) that runs `tapwright serve --keep-alive` with the flags
you give (`--apps`, `--accounts`, `--maps`, `--restart-gate`, ...); `tapwright service status`
and `tapwright service uninstall` check and remove it. The flags are also saved in
`~/.tapwright/service.json`: until `service uninstall`, every daemon started automatically (an
agent's call after `tapwright serve --stop`, say) uses them, checked again each time; if a file
they name has moved, serve.log says so, a plain `serve` starts, and `service status` says why. A
scheduled run's own vault is kept when the service names none. After `serve --stop` for an
upgrade, `schtasks /Run /TN Tapwright\serve` starts the service's daemon at once. On macOS and
Linux the command prints the launchd agent or systemd user unit to add, and saves nothing.

After the phone restarts, unlock it once with its passcode and enter the passcode once more when
it asks to allow UI automation. A daemon restart (`serve --stop`, an upgrade) more than about 8
hours after you last entered it asks once too, and a phone that locks by itself needs the
passcode in the vault (`--accounts`) to be unlocked. Otherwise it needs no one.

After a phone restart, the daemon starts no runner until the phone has been unlocked once (when
the phone says a passcode is needed). To turn that check off, start the daemon
with `--restart-gate off` (`tapwright service install --restart-gate off` carries it), or set
`TAPWRIGHT_RESTART_GATE=0` in the environment the daemon starts in (on Windows `setx
TAPWRIGHT_RESTART_GATE 0`, then `tapwright serve --stop` and `schtasks /Run /TN Tapwright\serve`).

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

### Recording the live view as a video

```sh
pip install "tapwright[video]"
tapwright record-video --out demo.mp4 --seconds 60
```

It records what the live view shows, in real time, until Ctrl+C or `--seconds`, and finishes the file either way.
It prints the file, its length and the number of frames. `tapwright serve` must be running (an agent's tool or
`tapwright view` starts it); the command never starts it, and needs no token from you.

- Options: `--out FILE.mp4` (required; the file must not exist), `--seconds N` (default: until Ctrl+C), `--fps N`
  (the most frames written a second, 1 to 60; default: every frame the live view sends, about 15 a second while the
  screen moves; a still screen sends few or none, and with the AirPlay `--video` feed none). If a burst is cut by
  `--fps`, its last picture is kept and lasts until the next one. If encoding falls behind, the oldest waiting frames
  are dropped (the command says how many) and the rest keep their real times.
- Timing: variable frame rate. Each picture is shown from the moment it was captured, so a still screen lasts as long
  as it really did and the last picture is held until you stop. Video time follows a clock a change of the computer's
  clock can't move. The picture's size is the first one's (full
  resolution, cut to even numbers); a rotated screen is fitted into it on black.
- `demo.frames.json` lists every frame: `seq`, `captured_ms` (epoch ms on this computer's clock), `captured_at` (the same
  moment as the action log writes it, `2026-11-17T15:23:31.916Z`) and `video_ms` (where it is in the video). The
  action log `~/.tapwright/log/actions-*.jsonl` uses the same clock, so a tap's `t` gives its place in the video:
  `video_ms` is its `t` minus the sidecar's `started_at`, or find the nearest frame by `captured_at`. A frame that was already on the screen
  when you started is at `video_ms` 0, with its real capture time.
- Ctrl+C is safe at any moment: the command finishes the file, with the frames still waiting. The video and
  `demo.frames.json` appear together, or not at all: if the file can't be finished (the disk is full) both are deleted
  and it says so; if an error cuts the recording short, what was recorded is saved and the error is said.
- Without PyAV the command stops with `pip install "tapwright[video]"` and records nothing (there is no fallback file).
- Privacy: **a recording shows whatever is on the phone's screen**: notifications, other apps, anything. Record test
  data, and look at the file before you share it. Only Tapwright's own reads of Notification Center (the
  `notifications` tool and notification triggers) are left out: a person or an agent who swipes it down by hand is
  recorded. The recording fails closed: from before the read until 3 seconds after its close was confirmed (or, when
  closing it could not be shown to work, until the next read closes it), it gets no frame, and none of a picture that
  was on screen at the end of the read; the video holds the last picture from before, and the command says
  "leaving frames out". It refuses to run against an older `tapwright serve` that can't do this (stop it with
  `tapwright serve --stop`). The token is read from the daemon's address file, goes only to a process that proved it
  holds it, and is never printed or written into the video or the sidecar.

## Troubleshooting

| Symptom | Fix |
|---|---|
| no iPhone on USB | unlock the phone; another cable or port; Windows: open Apple Devices once |
| "the profile doesn't include this iPhone" | run setup with the API key (it makes a new profile), or make one that lists the phone |
| WebDriverAgent doesn't start | phone unlocked and screen on; Developer Mode on; `tapwright doctor --fix` (or run setup again) to reinstall |
| the phone shows "Enter iPhone Passcode for XCTest" | enter it; then Settings > Developer > Enable UI Automation, so it doesn't ask again |
| works on USB, not on Wi-Fi | same network, no client isolation; run `tapwright probe` with the phone on the Wi-Fi |
| actions do nothing on a screen | look again: the screen may have changed; system alerts need `alert` |
| a look says `Elements: none` | the screen didn't answer in time (a video feed, a game). Work from its `o` refs, a point or a `{text}`; pause what plays or go to a still screen to read it whole |
| `pinch` or `press_button lock` says `unsupported` | they need touches from the computer: iOS 18 or later, and `TAPWRIGHT_HANDS` not `runner` |
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
