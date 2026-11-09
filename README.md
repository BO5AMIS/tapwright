# Tapwright

**Playwright-style testing on your real iPhone, driven by AI agents. From Windows, Linux or macOS.**

Tapwright gives an agent (Claude, Codex, anything that speaks MCP) eyes and hands on your
iPhone: it sees the screen as a list of elements plus a screenshot, and taps, types,
scrolls, swipes, answers permission prompts and opens deep links. It works with any app
on the phone: your TestFlight builds, development builds, the App Store version.
Nothing is added to your app, and no Mac or Xcode is needed.

> [!NOTE]
> Early. The parts that don't need a phone are tested (signing, the WebDriverAgent client,
> screen reading, the MCP server). The first end-to-end runs on a phone are next; see
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

**Give it to your agent.** Claude Code:

```sh
claude mcp add tapwright -- tapwright mcp
```

Other MCP clients: run `tapwright mcp` (stdio). Then ask, for example: *"Open MyApp on my
phone, sign up with a test email, and tell me if anything looks broken."*
[AGENTS.md](AGENTS.md) is the agent's guide; point your agent at it.

## Tools

| Tool | What it does |
|---|---|
| `list_apps` | the apps you installed (TestFlight and development builds included), with bundle ids |
| `launch_app`, `terminate_app` | open an app fresh, or close it |
| `look` | the screen: every visible element (type, label, id, position in points) and a screenshot |
| `tap`, `long_press` | by element id (`testID`/accessibility identifier), label, ref from the last look, or x/y |
| `type_text` | into a field (optionally clearing it first, or pressing return after) |
| `scroll`, `swipe` | lists and pages; drags between points |
| `alert` | accept, dismiss or read system alerts (permission prompts) |
| `press_button` | home, volume |
| `open_url` | deep links |
| `wait` | let something load, then look |

Every action returns the screen right after it, so each step is one call.

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
- About 22 frames per second at most (10 on the Light setting): fine for testing, not for
  judging animation smoothness.
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
- It's your phone: the agent guide tells agents to stay away from purchases, sending,
  deleting and account changes unless you asked for exactly that.

## Status

| | |
|---|---|
| Tested without a phone | WebDriverAgent download (pinned SHA-256) and re-signing with rcodesign, entitlements included; App Store Connect authentication; the WebDriverAgent HTTP client; screen reading and targeting; the MCP server |
| Needs a phone | setup end to end, the tunnel over USB and Wi-Fi, WebDriverAgent start-up, all actions; then the network/VPN mode |

## License

MIT. WebDriverAgent (BSD) and rcodesign (MPL-2.0) are downloaded at setup, not included.
Tapwright is not affiliated with Microsoft's Playwright or with Apple.
