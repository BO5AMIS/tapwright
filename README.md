# Tapwright

**Playwright-style testing on your real iPhone, driven by AI agents. From Windows, Linux or macOS.**

Tapwright gives an agent (Claude, Codex, anything that speaks MCP) eyes and hands on your
iPhone: it sees the screen as a list of elements plus a screenshot, and taps, types,
scrolls, swipes, answers permission prompts and opens deep links. It works with any app
on the phone: your TestFlight builds, development builds, the App Store version.
Nothing is added to your app, and no Mac or Xcode is needed.

> [!NOTE]
> Early. It works and is measured on one phone (an iPhone 14 Pro Max on iOS 26) from one
> Windows PC, over USB and Wi-Fi. Nobody but its authors has run it yet; see
> [Status](#status).

## How it works

```
 your computer                                              your iPhone
 ┌───────────────────────────┐   developer tunnel           ┌──────────────────────────┐
 │ agent ── MCP ── tapwright │  (USB, Wi-Fi, or any address │ WebDriverAgent           │
 │                           │   that reaches the phone)    │ (Apple's UI automation)  │
 │   pymobiledevice3 ────────┼─────────────────────────────▶│   taps, types, reads the │
 └───────────────────────────┘                              │   screen of any app      │
                                                            └──────────────────────────┘
```

Apple's UI-testing framework (XCTest) can drive any app on a phone in Developer Mode, as
long as a computer holds a developer connection to it. Tapwright runs
[WebDriverAgent](https://github.com/appium/WebDriverAgent) (the XCTest runner behind
Appium) on your phone, signed with your developer identity, and talks to it through
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3)'s tunnel. That tunnel needs
no admin rights and works on any OS.

Tapwright drives **the whole phone from the outside** (any app, system alerts, the home
screen). Its sibling project [Tapline](https://github.com/<you>/tapline) drives **your own
React Native app from the inside**, from anywhere, with nothing but the app and Tailscale.

## You need

- An iPhone on iOS 17 or later, and a USB cable for the one-time setup.
- A computer with Python 3.10+. On Windows, install **Apple Devices** from the Microsoft
  Store (it provides the USB connection); on Linux, `usbmuxd`.
- An Apple Developer Program membership, for an App Store Connect API key (setup then
  creates the certificate and profile itself). Or bring your own Apple Development
  certificate (.p12) and a development profile for a wildcard App ID that includes the
  phone.

## Quick start

```sh
pip install git+https://github.com/<you>/tapwright
```

**One-time setup**, phone plugged in and unlocked:

```sh
tapwright setup --asc-key-id KEY_ID --asc-issuer-id ISSUER_ID --asc-key-file AuthKey_KEY_ID.p8 \
               --bundle-prefix com.yourname.tapwright
```

It pairs with the phone (tap **Trust**), checks Developer Mode, creates a development
certificate and profile through the API (the private key stays on your computer), signs
and installs WebDriverAgent, and pairs for network use. Details and the manual option:
[docs/setup.md](docs/setup.md).

**Try it** on one of your apps:

```sh
tapwright smoke --app com.yourcompany.yourapp
```

It opens the app, prints its first screen as elements, saves a screenshot, and closes it.

**Give it to your agent**, naming the apps it may use. Claude Code:

```sh
claude mcp add tapwright -- tapwright mcp --apps com.yourcompany.yourapp
```

- `--apps` takes bundle ids or exact names, comma-separated. The agent is refused every
  other app (`not_allowed`) and is never shown another app's screen.
- On a phone kept for testing, with no personal accounts on it, use `--dedicated` instead:
  the agent may then use every app, the Home screen and system prompts.
- With neither, the agent can do nothing. That is deliberate: it is your phone.

Other MCP clients: run `tapwright mcp --apps …` (stdio). Then ask, for example: *"Open MyApp
on my phone, sign up with a test email, and tell me if anything looks broken."*
[AGENTS.md](AGENTS.md) is the agent's guide; point your agent at it.
[docs/quickstart.md](docs/quickstart.md) walks through a first test.

**Give your agent the skill.** It teaches an agent how to test with Tapwright: the loop, how
to read an answer, what to do on each error, how to report, and the mistakes agents make.

```sh
tapwright skill install               # for you: ~/.claude/skills and ~/.agents/skills
tapwright skill install --project .   # for one project
```

Claude Code reads `.claude/skills`; Codex and other agents read `.agents/skills`. The skill
is [skills/tapwright](skills/tapwright/SKILL.md), in the open
[Agent Skills](https://agentskills.io) format, so skill installers can take it from this
repository too.

## Tools

| Tool | What it does |
|---|---|
| `apps`, `open_app`, `close_app` | the apps you installed (TestFlight and development builds included); open one by bundle id or name, or close it |
| `look` | the screen: every visible element (role, text, id, position in points, what you can do with it) and a screenshot |
| `tap`, `long_press`, `toggle` | by element `{id}` (`testID`/accessibility identifier), `{label}`, `{text}`, `{ref}` from the last list, or `{x, y}` |
| `type_text` | into a field (clearing it first by default, or pressing return after) |
| `scroll`, `swipe`, `back` | lists and pages, to the start, the end, or straight to an element; drags between points; up one screen (an open sheet closes first) |
| `wait`, `wait_for` | a fixed delay, or until some text, id or label shows up or goes |
| `alert` | accept, dismiss or read system alerts (permission prompts) |
| `press_button` | home, volume |
| `open_url` | deep links |
| `viewer` | the link to the live view |

Every action answers with what changed on the screen, not the whole list, and says how long the
screen took to settle. Add `screenshot: true` for a picture. `tapwright mcp --screenshots DIR`
saves the pictures as files instead of sending them. Errors are `Error <code>: <message>`; for
`not_actionable` the first word says why (`disabled`, `off_screen`, `covered`, `under_tab_bar`,
`behind_alert`, `no_action`). The tools and their errors are the same as Tapline's:
[docs/AGENT-TOOLS.md](docs/AGENT-TOOLS.md).

## Live view

`tapwright serve` is a small background process that owns the connection to the phone.
Agent tools, the live view in your browser and any other client go through it, so you and
an agent can share one phone. Agent tools start it by themselves. To watch:

```sh
tapwright view      # opens the live view in your browser
```

You see the phone's screen live and can drive it: click to tap, hold to long-press, drag to
swipe, use the mouse wheel to scroll, and type into the focused field. The toolbar has Home,
volume, the stream quality and **Pause agent**. Your input goes first: the agent waits while
you drive. The live view is reachable from this computer only, with an access token in the
link.

- Drags play on the phone after you let go: WebDriverAgent can't follow a finger live.
- The picture is a stream of JPEG frames, about 26 a second while the screen changes and 5
  while it is still.
- `tapwright serve --video` plays the phone's own video instead, 60 frames a second, with
  nothing installed on the phone. The phone and the computer must be on the same Wi-Fi, and
  the computer needs an AirPlay receiver: see [docs/setup.md](docs/setup.md).
- The volume buttons press the phone's real buttons.

Other tools can use the same daemon: the agent tools over HTTP (`POST /api/<tool>`), and the
live view protocol in [docs/VIEWER-PROTOCOL.md](docs/VIEWER-PROTOCOL.md).

## Without the cable

After setup, unplug: on the same Wi-Fi, Tapwright finds the phone by itself. To reach the
phone over a VPN such as Tailscale, give its address:

```sh
tapwright probe --host 100.x.y.z --watch   # can the phone be reached there? (experimental)
tapwright serve --phone 100.x.y.z    # the daemon dials that address
```

Whether iOS accepts the developer connection through a VPN is what `probe` finds out.
Treat it as experimental until it has been confirmed on your phone.

## Limits

- Apple's automation sees what accessibility sees. Give important views accessibility
  identifiers (`testID` in React Native) and labels; games and custom-drawn screens show
  little beyond their screenshot.
- Face ID, Touch ID and the passcode can't be automated; unlock the phone yourself.
- Before it lets a UI test start, iOS may ask for the passcode ("Enable UI Automation").
  Tapwright waits and tells the agent. Turn on Settings > Developer > Enable UI Automation
  on a test device, or an unattended run stops there.
- It's your phone. An agent gets only the apps you name, and that is enforced. Inside those
  apps, the agent guide tells agents to stay away from purchases, sending, deleting and
  account changes unless you asked for exactly that: this part is guidance, not enforcement.
- Landscape isn't supported yet: the picture arrives sideways.

## Status

| | |
|---|---|
| Works on our phone | setup from Windows; USB and Wi-Fi; every tool; the live view as JPEG frames and as video; the app list (`--apps`) and `--dedicated` |
| Measured over USB | a tap, with its answer: 0.7 s; a look: 0.16 s; typing 1.1 s; a scroll 1.5 s; a person's tap in the live view shows after 0.27 s |
| Measured over Wi-Fi | slower: a tap 1.15 s, a look 0.38 s |
| Not done | landscape; a run over 5G or a VPN; a second phone or an iPad; a run by anyone but the authors |

The measurements, and the bar they are held against, are in
[docs/QUALITY-BAR.md](docs/QUALITY-BAR.md).

## License

MIT. WebDriverAgent (BSD) and rcodesign (MPL-2.0) are downloaded at setup, not included.
Tapwright is not affiliated with Microsoft's Playwright or with Apple.
