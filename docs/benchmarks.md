# Benchmarks

What Tapwright measures against, how, and the latest numbers. The targets are the shared
[quality bar](QUALITY-BAR.md), the same for Tapwright and Tapline. The runner is the shared benchmark in
Tapline's repository (`bench/`): it drives a tool through MCP exactly as an agent does.

## Setup

| | |
|---|---|
| Phone | iPhone 14 Pro Max, iOS 26.5, a test phone next to the computer |
| App | a production Expo app (React Native, new architecture) in a development build that includes the lab screen |
| Computer | Windows 11; the phone on USB, or on the same Wi-Fi |
| Tapwright | commit `871cf86`, 2026-11-11; WebDriverAgent on the phone, reached through a developer tunnel. Later commits (typing checks, the keyboard and status-bar rules, permission prompts in personal mode) were not re-measured |
| Mode | a dedicated device (`--dedicated`); in personal mode a tap costs about 0.1 s more (below) |
| Lab | one screen built for measuring: a state line the runner reads, buttons, inputs, a switch, a list of 40 rows, two sheets, an alert, a covered button, an animation |

The phone's notifications were switched off for these runs (per app, in Settings > Notifications). A
banner that comes down over the top of the screen at the moment of a touch can take it. Tapwright now
notices that, opens the app again and says so (`came to the front after the touch`), but a run with
banners measures the banners as well.

The runner writes one scorecard (JSON) per run. Times are taken on the computer with one clock.

## Speed (S), 2026-11-11

Twenty samples each; the median and the 95th percentile, in seconds. The full round trip: the MCP
request goes out, and the whole answer (with the change block) comes back.

| | USB median | USB p95 | Wi-Fi median | Wi-Fi p95 | Target (median / p95) |
|---|---|---|---|---|---|
| Tap (S1) | 0.65 | 0.68 | 1.03 | 1.55 | USB 0.7 / 1.2; Wi-Fi 1.0 / 1.5 |
| Type | 0.73 | 0.80 | 1.33 | 1.68 | USB 0.7 / 1.2; Wi-Fi 1.0 / 1.5 |
| Scroll, by a real touch | 1.46 | 1.85 | 1.77 | 2.39 | 1.5 / 2.0 |
| Toggle | 0.66 | 0.71 | 0.96 | 1.46 | USB 0.7 / 1.2; Wi-Fi 1.0 / 1.5 |
| Look, text only (S2) | 0.14 | 0.17 | 0.41 | 0.57 | 0.5 / 1 |
| Look with a screenshot (S3) | 0.18 | 0.21 | 0.38 | 0.74 | 0.8 / 1.5 |
| Start to ready (S5) | 1.98 | | 3.75 | | 5 |

- **Over USB** a tap, a toggle, a scroll and both looks meet the bar; typing is 26 ms over it.
- **Over Wi-Fi**, against Wi-Fi's own bar (1.0 / 1.5 s): the toggle meets it; a tap (1.03 / 1.55) and
  typing (1.33 / 1.68) are over it, and so is a scroll (1.77 / 2.39 against 1.5 / 2.0). Both looks meet
  the bar.
- **A scroll** is a real drag that stops where the finger stops (0.58 s), so the list doesn't keep
  moving under the next tap, plus WebDriverAgent's fixed cost per gesture and the read. iOS flings
  a drag whose last stretch is faster than about 330 pt/s. A drag slow enough not to fling can't
  finish in 0.7 s through WebDriverAgent.
- **A Wi-Fi tap** takes 0.8 to 1.0 s, depending on the network that minute (runs minutes apart
  gave 0.81 and 1.24 s). That is the main gap left, and most of it is not the link. Where it goes
  (traced 2026-11-11, `TAPWRIGHT_TRACE=1`, each comparison interleaved in one run):
  - **The touch, 0.47 to 0.52 s.** XCTest playing the touch on the phone takes the same time over
    USB and over Wi-Fi. It is 70 to 100 ms faster right after a tree read (Tapwright reads first, to
    find the target). WebDriverAgent's own `/wda/tap` is slower (0.83 s). The gap since the last
    request, the hold length and the live stream's quality change nothing measurable.
  - **The read for the answer, about 0.2 s.** It is mostly the phone building the tree (0.18 s over
    USB); Wi-Fi adds one round trip.
  - **Round trips, 20 to 60 ms each.** The phone's Wi-Fi radio dozes between packets. While the
    live stream runs (it does through a burst of agent calls) a round trip is 42 ms against 58, and a
    read 218 ms against 281. Keeping one connection open saves 10 to 15 ms a request over Wi-Fi, so
    it stays on over USB only.

  So a tap over Wi-Fi can't go much under 0.75 s through WebDriverAgent: the touch and the read alone
  are 0.7 s.
- **Personal mode** (only the apps you name) asks the phone which app is in front before every
  touch: a tap measured 0.78 s against 0.69 s on a dedicated device (2026-11-10). A look pays nothing.

## The live view (V), 2026-11-11

The viewer protocol's JPEG stream, read by the runner over the same link. Three runs over USB.

| | USB | Wi-Fi | Target |
|---|---|---|---|
| A person's tap to the picture (V1) | 0.23 s (p95 0.26 and 0.36; once 0.99) | 0.37 s (p95 0.61) | 0.3 s (p95 0.5) |
| Frames a second while the screen changes (V2) | 29 | 28 | 15 |
| Longest gap between frames (V3) | 0.05 to 0.19 s | 0.28 s | 0.25 s |
| The same while an agent looks in a loop | 0.10 to 0.19 s | 0.31 s | 0.25 s |
| Sharpness, three presets (V5) | 3 px per point | 3 px per point | 2.0 / 1.5 / 0.75 |
| An agent's tap while someone watches (V7) | 0.69 to 0.71 s | 1.02 s | 0.7 s (USB), 1.0 s (Wi-Fi) |
| The phone's CPU for the stream (V6 a) | about 27% of one core on a still screen with a viewer; about 88% under continuous animation | | 30% |

- **The phone's CPU** was measured with Apple's own sampler over the developer tunnel (DVT,
  2026-11-10). WebDriverAgent's screen captures cost about 2.6% of a core per frame a second.
  The stream runs at full rate only while something happens (an agent's call in the last 30 s, a
  person's input in the last 2 s, a change on screen in the last second), otherwise at 5 frames a second.
  Under continuous animation, 15 frames a second and 30% of a core can't both hold through these captures.
- **Video:** `tapwright serve --video` shows the phone's own Screen Mirroring at 60 frames a second and
  about 21 KB/s, played by the same viewer page. It costs the phone about +69% of one core under the lab
  animation. Not yet run through the benchmark.
- **The viewer page** is one file shared byte for byte with Tapline; its own measurements (first
  picture, click mark) are on Tapline's page.

## Reliability (R), 2026-11-11

Three full passes on each link; one miss in any pass fails the row.

| | Checks | USB misses | Wi-Fi misses |
|---|---|---|---|
| The right element (R1) | 48 | 0 | 0 |
| No false refusals (R2) | 48 | 0 | 0 |
| Refuses what a person couldn't do (R3) | 21 | 0 | 0 |
| A person's tap in the viewer lands where a finger would (R4) | 21 | 0 | 0 |
| Several matches are never a guess (R5) | 3 | 0 | 0 |
| The app frozen for 20 s (R6) | 1 | 0 | 0 |

R3 includes the checks Tapwright alone can make: refusing a button behind the iOS share sheet and
behind a system alert. Over Wi-Fi the developer tunnel sometimes leaves a request unanswered for 5 to
30 s. Tapwright asks a question again on a new connection, and answers a touch that got no answer with
`timeout … the action may have happened`, keeping the session.

## Agent use (A)

An agent that has never seen Tapwright (Claude Sonnet, given only what an MCP client shows, plus the
skill) tests the lab against an 11-point charter in personal mode. Every call is written down whole.

| Run | Calls | Calls only to read a state | Charter | Outside it |
|---|---|---|---|---|
| Before AGENT-TOOLS v1.5, no skill | about 110 | | 11 of 11 | nothing |
| Alone, run 1 | 44 | 2 | 11 of 11 | nothing |
| Alone, run 2 | 44 | 1 | 11 of 11 | nothing |
| With Tapline connected too | 40 (22 Tapwright, 18 Tapline) | 0 | 11 of 11 | nothing |

Target: the whole charter, at most 60 calls, at most 3 state-only reads, no misread answer,
nothing outside the charter. With both tools, Tapline never took one of Tapwright's 22 real touches for
a person's: Tapwright tells it before each one. These runs predate AGENT-TOOLS v1.7 (`placeholder=`) and
the typing check; they were not repeated since.

## Not measured

- Android, a second iPhone, an iPad, a TestFlight build.
- A computer other than this one: only Windows 11 was used.
- Video through the benchmark, and smoothness while capturing (V6 b).
- A phone away from the computer (Tailscale over another network): outside v1.
- A person judging the viewer, and a measured comparison with another tool.

## Run it yourself

With the phone set up (`tapwright setup`, see [setup.md](setup.md)), the lab app open, and a checkout of
Tapline's repository (the runner lives there, under `bench/`):

```
tapwright serve --keep-alive                      # USB; add --phone <address> for Wi-Fi
node bench/dist/cli.js --config tapwright.json --only speed      # or viewer, reliability
```

`tapwright.json` names the command the runner starts (`tapwright mcp --dedicated`), the phone, the
link and the lab's link. The runner and its README live in Tapline's repository under `bench/`.
