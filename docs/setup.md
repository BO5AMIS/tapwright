# Setup

Once per phone, with the phone on USB. About ten minutes.

## 1. The computer

- Python 3.10 or newer, then `pip install git+https://github.com/<you>/tapwright`.
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

## 5. The live view

```sh
tapwright view
```

opens the phone's screen in your browser, live, and lets you drive it. It starts
`tapwright serve` (the background process that owns the phone) if it isn't running; agent
tools start it too. It stops by itself after 30 minutes without viewers or requests
(`tapwright serve --keep-alive` keeps it running), or with `tapwright serve --stop`. Its log
is `~/.tapwright/serve.log`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| no iPhone on USB | unlock the phone; another cable or port; Windows: open Apple Devices once |
| "the profile doesn't include this iPhone" | run setup with the API key (it makes a new profile), or make one that lists the phone |
| WebDriverAgent doesn't start | phone unlocked and screen on; Developer Mode on; run setup again to reinstall |
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
