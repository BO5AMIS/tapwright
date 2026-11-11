# Setup

Once per phone, with the phone on USB. About ten minutes.

## 1. The computer

- Python 3.10 or newer, then `pip install git+https://github.com/<owner>/tapwright`.
- Windows: **Apple Devices** from the Microsoft Store (or iTunes). It provides the USB
  connection to the phone.
- Linux: `usbmuxd` (`sudo apt install usbmuxd`).
- macOS: nothing else.

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

## 4. Check

```sh
tapwright doctor
tapwright smoke --app <bundle id of one of your apps>
```

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
| WebDriverAgent doesn't start | phone unlocked and screen on; Developer Mode on; run setup again to reinstall |
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
