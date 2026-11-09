# Quality bar

Shared by Tapline and Tapwright. Version 1, agreed by both projects and approved by the user on
2026-11-09. Each repo keeps an identical copy.

The two tools should feel like one product made by one team. They differ only in setup: how an
agent is wired to them, how the user gives access (an app pairs with Tapline; a whole phone
connects to Tapwright), and the connection itself. Everything an agent or a person
experiences meets the same targets in both tools: speed, reliability, screenshots, the live
picture, the controls and the look.

Where a tool doesn't meet a target yet, the target stays the same and the gap is listed at
the end with a plan. There are no per-tool targets.

## Links

- **Good link:** what each tool normally uses. For Tapline, Tailscale over Wi-Fi or LTE. For
  Tapwright, USB or Wi-Fi.
- **3G:** the iPhone's Network Link Conditioner "3G" profile on the phone (Settings →
  Developer): about 780 kbit/s to the phone, 330 kbit/s from it, 100 ms delay. A 3G target
  means the tool still works, slower: every action completes, the picture keeps coming, and
  nothing times out. Turning the profile on changes a setting on the user's phone, so it's
  the user's call each time.

## How it's measured

- **The fixtures:** a lab screen in the test app's test branch, opened by deep link. It is
  JavaScript only and needs a build that contains it on the phone (a development build is
  enough). Its state is on screen in a text element with `testID="lab-state"`, as a label
  such as `pressed=Save typed=abc switch=on taps=12 slow=0`. Both tools score through their
  normal look, so the checks are the same for both. Tapline may also read the state through
  a test command.
- **The benchmark:** one runner for both tools. It drives each tool through MCP over stdio,
  exactly as an agent does, and the viewer through VIEWER-PROTOCOL.md. It records the phone
  model, iOS version, tool version, link and date, and writes one scorecard JSON per run (see
  the end).
- **Samples:** at least 20 per speed metric, reported as median and p95. The reliability
  suite runs 3 times per link, and a single miss fails it.
- **Times** are taken on the computer that runs the tool, with one clock.

## Reliability (R): every check, every run, both links

| | Standard | Measured by |
|---|---|---|
| R1 | **Right element.** An action changes exactly what it targets and nothing else. | Each lab action, then lab-state compared with the expected state. |
| R2 | **No false refusals.** What a person could do, the tool does. | The same checks: 0 refusals of reachable targets. |
| R3 | **Never silently something else.** When a person couldn't reach the target (off screen, covered by a sheet or overlay, under a bar the OS draws, disabled, behind a system alert), the tool doesn't act on something else. It refuses with `not_actionable` and names what's in the way, or, for a tap on the real screen, reports what the tap landed on. | Lab fixtures for each case: lab-state unchanged, the code and the reason present. |
| R4 | **The viewer's input lands where a real touch would.** A click in the viewer at a point does what a finger at that point does. | A grid of points over the lab: inside a sheet, on the tab bar, on buttons, near the edges, on empty space. Viewer taps and real taps (or the tool's own tap at that point) give the same lab-state. |
| R5 | **No silent guesses.** When a target matches several elements and no `index` is given, the tool refuses with `ambiguous` and lists the candidates. It never picks one silently (AGENT-TOOLS.md). | Two lab buttons with the same label. |
| R6 | **Drops end clearly.** A request in flight when the link or the app drops ends with a code (`reconnecting`, `timeout`, `in_background`, `locked`), never a false success. After the app or phone comes back, the next action works with no step from the user beyond reopening it. | Background the app or cut the link mid-action, then bring it back and act. |
| R7 | **No harm to the phone or the app.** No JavaScript errors, warnings or crashes caused by the tool, and no iOS kill reports for the tool's processes (`cpu_resource_fatal` and the like, from `pymobiledevice3 crash ls`). | The app's logs and the crash list after each run. |
| R8 | **3G.** R1–R7 pass on the 3G profile with no timeouts. | The suite on 3G. |

## Speed (S): medians and p95

| | Standard, from → to | Good link | 3G |
|---|---|---|---|
| S1 | **Action round trip:** the MCP request for a tap, type or scroll is written → its full answer arrives, the default answer included. | median ≤ 0.7 s, p95 ≤ 1.2 s | median ≤ 1.5 s, p95 ≤ 2.5 s |
| S2 | **Look, text only:** request → the element list arrives. Measured on the lab screen and on a busy real screen (about 50 elements). | median ≤ 0.5 s, p95 ≤ 1.0 s | median ≤ 1.5 s, p95 ≤ 3.0 s |
| S3 | **Look with the default screenshot:** request → list and image arrive. | median ≤ 0.8 s, p95 ≤ 1.5 s | median ≤ 2.5 s, p95 ≤ 4.0 s |
| S4 | **Reconnect:** the app or phone is back in front, or the link is back → the first successful look. | ≤ 3 s | ≤ 6 s |
| S5 | **Start to ready:** the MCP server (or `serve`) starts, with the app or phone open and paired → the first successful look. | ≤ 5 s | ≤ 8 s |

## The live picture (V)

| | Standard, from → to | Good link | 3G |
|---|---|---|---|
| V1 | **Tap to picture:** the viewer sends a person's tap on /ws → it receives the first /stream frame showing the result. The lab's tap counter area changes; a fixed crop counts as changed above a pixel threshold, since a one-digit change moves a crop's mean by only about 3–7 of 255. | median ≤ 0.3 s, p95 ≤ 0.5 s | median ≤ 1.0 s, p95 ≤ 2.0 s |
| V2 | **Frame rate while the screen changes:** frames received per second while the lab animation runs, over 10 s. The bar rises to 20 fps once both tools measure that under animation. | ≥ 15 fps | ≥ 2 fps |
| V3 | **Longest stall** while the app is live and the animation runs: the longest gap between frames received. Measured also while an agent's look runs, since that can keep the phone side busy. Any stall over 3 s is explained by the status line (reconnecting, paused on the phone …). | ≤ 0.25 s | ≤ 2 s |
| V4 | **Frame age:** received → `X-Captured-At`, on the server's clock. Not applicable where the server has no capture time (Tapwright: WebDriverAgent gives none, so its `X-Captured-At` is the arrival time); V1 covers that case. | median ≤ 0.15 s | median ≤ 1.0 s |
| V5 | **Sharpness:** frame width in pixels per screen point, per preset. Both tools offer the same presets, and each preset gives the same sharpness and frame rate. | sharp ≥ 2.0 px/pt, balanced ≥ 1.5, light ≥ 0.75 | the tool may lower the preset to keep V2 and V7, and says so in the status |
| V6 | **The phone doesn't pay for it.** (a) CPU: the whole phone's CPU over all processes with the viewer on, minus the same with it off, averages ≤ 30% of one core over 60 s at the good-link rate (DVT sysmontap `cpuUsage`). The whole phone, because capture work can run in iOS's own processes, not only in the tool's. No single process of the tool comes near iOS's 80% background limit. (b) Smoothness: the lab animation's slow frames (over 50 ms) with the viewer on are at most 1 percentage point above the viewer off (lab-state `slow=`). | as stated | as stated |
| V7 | **Bandwidth fits the link.** Frames use at most 80% of the phone's upload, and the agent's actions still meet S1 while the viewer runs: the picture never starves the agent. | as stated | ≤ 33 KB/s of frames |
| V8 | **The page.** First frame visible after opening the link; the status line follows a change; a person's click shows a mark at once (locally, before the server answers); no console errors; current Chrome, Edge, Firefox and Safari. | first frame ≤ 1.5 s, status ≤ 2 s, mark ≤ 100 ms | first frame ≤ 4 s, status ≤ 2 s, mark ≤ 100 ms |

## Screenshots for agents (Q)

| | Standard | Measured by |
|---|---|---|
| Q1 | **Readable:** the default screenshot has at least 1.3 px per screen point (600 px wide on a 440-point phone), as JPEG, and the lab's 11-point fine print reads correctly. | The image width, plus a small checker model reading the fine print exactly. |
| Q2 | **The same everywhere,** as AGENT-TOOLS.md says: 600 px wide by default, JPEG at about 0.75, and no image after an action unless `screenshot: true`. | A diff of the two tools' answers on the lab. |

## Consistency (C)

| | Standard |
|---|---|
| C1 | One agent interface: AGENT-TOOLS.md (tool names, parameters, answer shapes, element lines, codes). Extras only where a tool can do something the other can't. |
| C2 | One viewer page, byte-identical in both repos; differences only through `accepts`. |
| C3 | The same error codes and message style: a code, then plain words saying what happened and what to do. |
| C4 | The same guide voice in README and AGENTS.md: setup sections differ, everything after is shared. |

## Gaps today, with plans

| Tool | Standard | Today | Plan |
|---|---|---|---|
| Tapline | V2 frame rate | about 1 fps idle, about 3 fps for 1.2 s after an action (screenshots from JavaScript, 30–65 ms each) | native capture with ReplayKit in the app's own process, as an optional module; needs EAS builds |
| Tapline | V5 sharpness | balanced is 400 px on a 440-point phone, about 0.9 px/pt | with native capture, balanced at ≥ 660 px |
| Tapline | R3 bars | iOS's native tab bar is placed by rule on an iPhone in portrait; a native navigation bar can still cover an element unnoticed | measure the navigation bar's area the same way, or read it from React Native Screens |
| Tapline | R4 gestures | a person's drag in the viewer becomes a scroll; controls that react only to gestures (swipe rows, sliders, pinch) don't respond | say so in the viewer (`unsupported`) rather than scroll; gesture events later |
| Tapline | V1, S1–S5, V6 | not measured yet | the benchmark's first run |
| Tapwright | S2 look | 1.5–7 s (WebDriverAgent `/source`) | measure WebDriverAgent's source options: leave out costly attributes (`visible` is the known slow one), limit the snapshot depth, JSON against XML, query only actionable elements. Whether 0.5 s is reachable on a busy screen is unknown; numbers go here |
| Tapwright | S1 action | about 1.1 s, including a fixed 0.6 s settle; the default change list needs a fresh tree, so S1 waits on S2 | settle when the picture stops changing, read the tree faster (S2), and when the stream shows no change after the settle, answer `No visible change.` without reading the tree |
| Tapwright | S5 start to ready | about 6 s from `serve` to live (tunnel about 1.7 s, WebDriverAgent about 3.5 s), plus the first look; a runner left from an earlier `serve` answers for about 15 s, then hangs (measured), so it can't be reused | profile, then trim |
| Tapwright | Q1 screenshots | up to 400 px, about 0.93 px/pt | 600 px from the stream's newest frame, resized on the PC (AGENT-TOOLS.md) |
| Tapwright | R3 refusals | none today | refuse `disabled` and `behind_alert` first (cheap); `covered` needs WebDriverAgent's `hittable` per action, measured before promised; "what the tap landed on" comes from the change list |
| Tapwright | R4, V landscape | the phone can't afford to rotate frames (103% of a core, measured), so landscape frames arrive sideways and viewer input maps wrong | rotate on the PC (about 28 ms a frame) and track the orientation |
| Tapwright | V2, V7 on 3G (only when the phone is far away over Tailscale; USB and Wi-Fi are local) | at 33 KB/s, 2 fps needs frames of about 16 KB; the smallest scaled frame measured was 58 KB (a third of full size, quality 25; WebDriverAgent seems to re-encode scaled frames at high quality) | measure the smallest affordable frame; expect about 0.5 fps; a capture path of Tapwright's own is a long-term option |
| Both | R1–R8, V on 3G | not measured yet | the benchmark on 3G, with the user's OK for the profile |

## Scorecard

One JSON file per run:

```json
{
  "tool": "tapline", "version": "0.1.0", "phone": "iPhone …", "ios": "26.5", "link": "good",
  "date": "2026-11-10T…",
  "results": [
    { "id": "S1", "samples": 20, "median": 0.62, "p95": 1.05, "target": { "median": 0.7, "p95": 1.2 }, "pass": true },
    { "id": "R1", "checks": 40, "misses": 0, "pass": true }
  ]
}
```

## Changing the bar

The bar changes only by agreement between the two projects and the user's approval, like
VIEWER-PROTOCOL.md. A target can rise any time both tools meet it; it doesn't fall because a
tool can't meet it.
