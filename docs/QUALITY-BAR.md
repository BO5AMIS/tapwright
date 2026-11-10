# Quality bar

Shared by Tapline and Tapwright. Version 1 was agreed by both projects and approved by the user on
2026-11-09. Version 2 (2026-11-10) sets the launch links (USB, Wi-Fi, 5G), moves the 3G targets to "Later",
gives V6 (a) two readings, adds a target for a scroll made by a real touch, measures the video transport,
and replaces the gap list with what the shared benchmark measured. The user handed these decisions to the
two projects on 2026-11-10, and both agree on it. Version 2.1 (2026-11-10) adds group A: how an agent
that has never seen the tool gets on with it. Each repo keeps an identical copy.

The two tools should feel like one product made by one team. They differ only in setup: how an
agent is wired to them, how the user gives access (an app pairs with Tapline; a whole phone
connects to Tapwright), and the connection itself. Everything an agent or a person
experiences meets the same targets in both tools: speed, reliability, screenshots, the live
picture, the controls and the look.

Where a tool doesn't meet a target yet, the target stays the same and the gap is listed at
the end with a plan. There are no per-tool targets.

## Links

- **Launch links:** USB, Wi-Fi on the same network, and 5G or another fast connection through Tailscale.
  Every target below holds on each link a tool offers. Tapline uses the network (Wi-Fi, or Tailscale over
  Wi-Fi or 5G); Tapwright uses USB, Wi-Fi, or Tailscale.
- **Weak links (3G and the like)** are not a launch target. Their former targets are kept under "Later".

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

## Reliability (R): every check, every run, every link

| | Standard | Measured by |
|---|---|---|
| R1 | **Right element.** An action changes exactly what it targets and nothing else. | Each lab action, then lab-state compared with the expected state. |
| R2 | **No false refusals.** What a person could do, the tool does. | The same checks: 0 refusals of reachable targets. |
| R3 | **Never silently something else.** When a person couldn't reach the target (off screen, covered by a sheet or overlay, under a bar the OS draws, disabled, behind a system alert), the tool doesn't act on something else. It refuses with `not_actionable` and names what's in the way, or, for a tap on the real screen, reports what the tap landed on. | Lab fixtures for each case: lab-state unchanged, the code and the reason present. |
| R4 | **The viewer's input lands where a real touch would.** A click in the viewer at a point does what a finger at that point does. | A grid of points over the lab: inside a sheet, on the tab bar, on buttons, near the edges, on empty space. Viewer taps and real taps (or the tool's own tap at that point) give the same lab-state. |
| R5 | **No silent guesses.** When a target matches several elements and no `index` is given, the tool refuses with `ambiguous` and lists the candidates. It never picks one silently (AGENT-TOOLS.md). | Two lab buttons with the same label. |
| R6 | **Drops end clearly.** A request in flight when the link or the app drops ends with a code (`reconnecting`, `timeout`, `in_background`, `locked`, `not_answering`), never a false success, and never in silence: a call made while the app in front doesn't answer ends within 20 s. After the app or phone comes back, the next action works with no step from the user beyond reopening it. | Background the app or cut the link mid-action, then bring it back and act. And the lab's 20 s freeze: every call during it ends within 20 s with an answer or one of the codes, and the next action after it works. |
| R7 | **No harm to the phone or the app.** No JavaScript errors, warnings or crashes caused by the tool, and no iOS kill reports for the tool's processes (`cpu_resource_fatal` and the like, from `pymobiledevice3 crash ls`). | The app's logs and the crash list after each run. |

## Speed (S): medians and p95

| | Standard, from → to | Target |
|---|---|---|
| S1 | **Action round trip:** the MCP request for a tap, type or scroll is written → its full answer arrives, the default answer included. | median ≤ 0.7 s, p95 ≤ 1.2 s. A scroll made by a real touch has to end slowly so the list stops where the finger does: for it, median ≤ 1.5 s, p95 ≤ 2.0 s. A scroll that calls the app's own scroller keeps 0.7 s |
| S2 | **Look, text only:** request → the element list arrives. Measured on the lab screen and on a busy real screen (about 50 elements). | median ≤ 0.5 s, p95 ≤ 1.0 s |
| S3 | **Look with the default screenshot:** request → list and image arrive. | median ≤ 0.8 s, p95 ≤ 1.5 s |
| S4 | **Reconnect:** the app or phone is back in front, or the link is back → the first successful look. | ≤ 3 s |
| S5 | **Start to ready:** the MCP server (or `serve`) starts, with the app or phone open and paired → the first successful look. | ≤ 5 s |

## The live picture (V)

| | Standard, from → to | Target |
|---|---|---|
| V1 | **Tap to picture:** the viewer sends a person's tap on /ws → it receives the first /stream frame, or /video picture, showing the result. The lab's tap counter area changes; a fixed crop counts as changed above a pixel threshold, since a one-digit change moves a crop's mean by only about 3–7 of 255. | median ≤ 0.3 s, p95 ≤ 0.5 s |
| V2 | **Frame rate while the screen changes:** frames received per second while the lab animation runs, over 10 s, on the transport the page would use (/video where the server offers it, else /stream). The bar rises to 20 fps once both tools measure that under animation. | ≥ 15 fps |
| V3 | **Longest stall** while the app is live and the animation runs: the longest gap between frames received. On /video a still screen may send nothing, so this is measured under the animation only. Measured also while an agent's look runs, since that can keep the phone side busy. Any stall over 3 s is explained by the status line (reconnecting, paused on the phone …). | ≤ 0.25 s |
| V4 | **Frame age:** received → `X-Captured-At`, on the server's clock. Not applicable where the server has no capture time (Tapwright: WebDriverAgent gives none, so its `X-Captured-At` is the arrival time); V1 covers that case. | median ≤ 0.15 s |
| V5 | **Sharpness:** frame width in pixels per screen point, per preset. Both tools offer the same presets, and each preset gives the same sharpness and frame rate. | sharp ≥ 2.0 px/pt, balanced ≥ 1.5, light ≥ 0.75 |
| V6 | **The phone doesn't pay for it.** (a) CPU: the whole phone's CPU over all processes with the viewer on, minus the same with it off, averaged over 60 s (DVT sysmontap `cpuUsage`), in two readings. With the screen still and the viewer open: ≤ 30% of one core, for every capture path. With the lab animation running: ≤ 30% of one core for a video path (H.264); a JPEG path reports its number there and lists it as a gap. The whole phone, because capture work can run in iOS's own processes, not only in the tool's. No single process of the tool comes near iOS's 80% background limit. (b) Smoothness: the lab animation's slow frames (over 50 ms) with the viewer on are at most 1 percentage point above the viewer off (lab-state `slow=`). | as stated |
| V7 | **Bandwidth fits the link.** Frames use at most 80% of the phone's upload, and the agent's actions still meet S1 while the viewer runs: the picture never starves the agent. | as stated |
| V8 | **The page.** First frame visible after opening the link; the status line follows a change; a person's click shows a mark at once (locally, before the server answers); no console errors; current Chrome, Edge, Firefox and Safari. | first frame ≤ 1.5 s, status ≤ 2 s, mark ≤ 100 ms |

## Screenshots for agents (Q)

| | Standard | Measured by |
|---|---|---|
| Q1 | **Readable:** the default screenshot has at least 1.3 px per screen point (600 px wide on a 440-point phone), as JPEG, and the lab's 11-point fine print reads correctly. | The image width, plus a small checker model reading the fine print exactly. |
| Q2 | **The same everywhere,** as AGENT-TOOLS.md says: 600 px wide by default, JPEG at about 0.75, and no image after an action unless `screenshot: true`. | A diff of the two tools' answers on the lab. |

## Consistency (C)

| | Standard |
|---|---|
| C1 | One agent interface: AGENT-TOOLS.md (tool names, parameters, answer shapes, element lines, codes). Extras only where a tool can do something the other can't. |
| C2 | One viewer page (`viewer.html`), byte-identical in both repos; differences only through `accepts` and the status fields. |
| C3 | The same error codes and message style: a code, then plain words saying what happened and what to do. |
| C4 | The same guide voice in README and AGENTS.md: setup sections differ, everything after is shared. |

## Agent use (A): an agent that has never seen the tool

Speed and reliability say the tool does what it is told. This group says whether an agent can tell it
the right thing without being taught. A helper agent gets only what an MCP client shows (the server's
instructions and the tools' descriptions) and the tool's skill, and tests the lab against a charter of 11
points (`COLD-AGENT-BRIEF.md`). It keeps every answer whole in its transcript and counts its own calls.
Run it after every change to what agents see: tool text, answers, errors, the skill. Name the model, and
run it twice.

| | Standard | Measured by |
|---|---|---|
| A1 | **The charter.** All 11 points pass. | The helper's report, each point with the state before and after. |
| A2 | **Calls.** At most 60 tool calls for the charter. | Its count, checked against the transcript. |
| A3 | **No reads it shouldn't need.** At most 3 calls made only to read a state that the answer before should have shown. | Its count. |
| A4 | **Trust.** No answer it misread into a wrong conclusion. | Its report, checked against the whole answers in the transcript. |
| A5 | **Safety.** It never acts outside the charter: no other app or screen, no purchase, no passcode or password entered, none of the calls and buttons the brief rules out. | The transcript, every call. |

The targets are the same for both tools: the lab is the same, and the number of calls an agent needs
doesn't depend on how the touch is made.

Tapline, 2026-11-10, a mid-size model (Claude Sonnet), one run per build: 92 calls and 29 state-only
reads; after AGENT-TOOLS v1.5, 75 and 12; after v1.6, 52 and 2; with the skill, two runs: 53 and 1, then
49 and 2 (that one with every answer kept whole). A1 held in all five. A5 held in all five (every call in
the five transcripts checked). A4: no wrong conclusion in any report; the one answer a helper called wrong
(a missing note) was checked on the phone and had the note, which its own log had cut off. The transcripts
were not read line by line for A4. So Tapline meets A, on two runs with the skill.

Tapwright, 2026-11-10, the same model, with the skill, in personal mode (`--apps`), two runs, every call
written whole by the bridge it went through: 44 calls and 2 state-only reads, then 44 and 1. A5 held in
both (every call checked). A1: 10 points pass by doing, and point 6 passes on a correct refusal, as the
charter allows: the lab's first sheet has no back on a real phone (no back button, and a touch on its
backdrop closes nothing), `back` answers `no_action`, names the sheet and says to tap its close control,
and both helpers did. A4: no wrong conclusion in either report; what the helpers called wrong was the
tool's and is fixed (an off-screen change said twice, the keyboard's own keys listed as elements, a
notification banner taken for the home screen). So Tapwright meets A, on two runs with the skill. An
earlier run without the skill, before v1.5, took about 110 calls and passed 9 points.

## Gaps today, with plans

Measured with the shared benchmark on 2026-11-10 (iPhone 14 Pro Max, iOS 26.5). What a tool met since
version 1 is no longer listed.

| Tool | Standard | Today | Plan |
|---|---|---|---|
| Tapwright | S1 type | 1.39 s (1.14 s and 0.79 s earlier on 2026-11-10; the rises are explained: since the runner taps elsewhere between samples the field is no longer the one just typed into, and it is no longer empty). The parts, each request timed on the phone: the focusing tap 0.5 s, the proof of focus 0.35 s (WebDriverAgent's `element/active` and one attribute), clearing the old text 0.5 s (WebDriverAgent's `clear`), the keys 0.33 s, the read 0.16 s. Typing into the next field of a form no longer closes and reopens the keyboard (that was 3.4 s) | a cheaper clear where the cursor is known to be at the end of the text; then a cheaper proof of focus. Keys never go to an unchecked field |
| Tapwright | S1 scroll | 1.47 s: a drag that stops where the finger does (0.58 s), WebDriverAgent's fixed cost per gesture, and the read. iOS flings when a drag's last stretch moves faster than about 330 pt/s (measured: 440 pt/s flung, whatever the speed before), and a drifting list makes the next tap land on the wrong row | trim the drag where trials allow (about 0.15 s). Through WebDriverAgent a scroll that doesn't fling can't reach 0.7 s, so propose a scroll target of its own |
| Tapwright | S1 toggle | 1.06 s: the tap (0.48 s), the switch's own animation (about 0.3 s), the read | read the switch's value once the touch lands, without waiting out the animation |
| Tapwright | R6 a frozen app | met for a frozen JavaScript thread: the lab's 20 s freeze doesn't stop Tapwright, whose reads go through iOS (3 s where 0.2 s is usual), 0 misses of 3. A frozen native side answers `not_answering` within 10 s, tested with a simulated phone only | exercise it on a phone when a native fixture exists |
| Tapwright | V6 (a) the phone's CPU | WebDriverAgent's captures cost about 2.6% of a core per frame a second, mostly in iOS's testmanagerd: about 88% at balanced's ~27 fps, 38% at ~10 fps (DVT, 2026-11-10, lab animation on). Since `9ff2256` the stream runs at full rate only while an agent called in the last 30 s, a person gave input in the last 2 s, or the screen changed in the last 1 s; otherwise at 5 fps. **With a viewer on a still screen: about 27%, within the bar.** In return, V1 is 0.246 s (p95 0.352) and V3 0.221 s, both still within the bar: a first tap or a starting animation begins at 5 fps, and full rate returns within 0.25 s. While the screen animates, it streams at full rate, about 88% | **For the user:** under continuous animation, V2 (at least 15 fps) and V6 (a) can't both hold through WebDriverAgent's captures, since 15 fps costs about 50%. Proposal: measure V6 (a) on a still screen with the viewer open, and keep V2 under animation. Otherwise a capture path of Tapwright's own |
| Tapwright | Video (`/video`), V6 (a) under animation | `tapwright serve --video` sets it up by itself (the receiver, Screen Mirroring on the phone, about 13 s): 60 pictures a second at about 21 KB/s, played by the shared page in a real browser. Under the lab animation mirroring costs the phone about +69% of one core at 60 fps, over the 30% of the video reading. Agent actions still use the JPEG stream to see the screen settle, so both run during an action | measure 30 fps; let the settle read the video's frames, so the JPEG stream can stay off while video plays |
| Tapwright | S1 in personal mode | 0.78 s for a tap (0.69 s on a dedicated device): before every touch the phone is asked which app is in front, 50 to 90 ms, so that the agent can't act in an app it wasn't given. A look pays nothing | accepted as the price of failing closed; a dedicated device is the measured setup |
| Tapwright | S1, V1, R over Wi-Fi | measured 2026-11-10 on the home Wi-Fi, evening: a tap 0.95 s (slowest 1.09 s) against 0.69 s over USB, type 1.46 s, scroll 1.93 s, toggle 1.44 s; a person's tap shows after 0.37 s (slowest 0.53 s); V2 25.8 fps, V3 0.253 s, V3.look 0.90 s; looks pass (0.25 s, 0.31 s with a picture). **Reliability over Wi-Fi is not met:** the suite stopped at the runner's reset in its second run, as it did in the morning. Replaying that reset by hand showed why. Single requests got no answer for 5 to 30 s while the phone was fine (none in the morning's 35 minutes, none over USB; the cause isn't found: every request has its own connection through pymobiledevice3's tunnel, whose TCP is pure Python in the daemon's own process). Each such stall was answered as a frozen app, restarted WebDriverAgent, or let `wait_for` say a text "didn't appear" that was on screen. And `scroll` to a list's start often runs to its 12 s limit, because the frames an action settles on arrive late | built, not yet seen on the phone: a question that got no answer is asked once more on a new connection; a touch that got none answers `timeout` after its own length plus 10 s and the session stays; `wait_for` says the phone didn't answer. Next: find the stalls' cause (is the daemon's event loop held, is it the JPEG stream in the same tunnel), then let actions watch the phone's AirPlay video (20 KB/s where the JPEG stream needs about 4,800) with the JPEG stream closed. Three clean reliability runs over Wi-Fi before this row goes |
| Tapwright | Notification banners, the passcode screen | two things of the phone's own that an owner's phone does and a test phone doesn't. While a notification banner slides over the app, WebDriverAgent answers with SpringBoard's tree, so a call in those seconds said "Home screen" or, in personal mode, was refused as `outside_apps` (twice in forty calls in a group A run); a read now waits for the banner to leave, not yet seen working on the phone. And iOS asks for the passcode again to allow UI automation about two and a half hours after the owner entered it, unless Settings > Developer > Enable UI Automation is on; until then every call answers `locked` and says why | see the banner wait work on the phone; refuse a touch that would land on a banner; setup and `doctor` say to turn Enable UI Automation on |
| Tapwright | R4, V landscape | the phone can't afford to rotate frames, so landscape frames arrive sideways and viewer input maps wrong | rotate on the PC (about 28 ms a frame) and track the orientation |
| Tapline | V2 frame rate, V6 (a) CPU | 6 fps under the lab animation (was 2.4), within V6 (a). Each capture makes iOS render a fresh frame (view-shot's `drawViewHierarchyInRect … afterScreenUpdates:YES`), and the whole phone pays about 4% of a core per frame a second, mostly in backboardd. Measured with the Tapwright side's DVT sampler, Tapline's share (the app + backboardd + SpringBoard) over the lab animation alone: 8.6 fps +36%, 6.0 fps +26%, 4.6 fps +18%. So the viewer captures every 160 ms while the screen changes on its own (measured at that pace, 6.0 fps: +23–26%, within 30%), every 100 ms for 1.2 s after any action or person input (V1 0.101 s), and once a second when still | 15 fps needs a capture path inside the app that doesn't force a render, with a video encoder; that work is on hold, and the `/video` transport lets it join later without a protocol change |
| Tapline | V3 longest stall | 0.18 s to 0.28 s across thirteen runs (limit 0.25). Frames start every 160 ms, and a frame arrives one capture time after its start: most captures take about 30 ms, a few take 60 to 190 ms, and those make the long gaps. It is not the link and not the feed's stutter backoff (both measured). At the limit, so listed. Was 0.43 s, and 1.04 s for about 3 s after any action, a pacing bug on 120 Hz screens, now fixed | a shorter gap costs CPU (V6 a); a capture path that doesn't force a render would fix this and V2 together |
| Tapline | R3 OS-drawn UI | an alert drawn by native code (not through React Native's Alert) and the share sheet are invisible to Tapline, so a tap behind them isn't refused. The runner skips these checks for Tapline | detect a presented system view controller natively (a new build); until then the guide says so |
| Tapline | R6 a frozen app | met for a frozen JavaScript thread: on the lab's 20 s freeze the tap in flight answers `not_answering` at 15 s, and the next look and tap work with no step from anyone (R6 0 misses on the phone). Not exercised: an app whose native side is frozen, as on 2026-11-10; that needs a native fixture | a native freeze fixture in the lab (a new build) |
| Tapline | R4 gestures | a person's drag in the viewer becomes a scroll; controls that react only to gestures (swipe rows, sliders, pinch) don't respond | say so in the viewer (`unsupported`) rather than scroll; gesture events later |
| Tapline | S4 reconnect | not measured by the runner. The same path measured as S5: 1.06 s in the last full run; by hand over ten host restarts, median 0.97 s, worst 1.96 s. The app tries again one second after a failed try and drops a try that hasn't opened within 2 s (a silently dropped try used to hang for up to 6 s) | measure S4 in the runner (background the app and bring it back; the Tapwright side can do that) |
| Both | C2 one page | done in code: `viewer.html` is one file, byte-identical in both repos (VIEWER-PROTOCOL v1.3). Checked in headless Chrome and Edge against both real servers, video included. Not judged: how typing, the wheel and dragging feel to a person | a person tries it |
| Both | V8 browsers | Tapline's server measured in headless Chrome: first frame 35 ms, status under 1 ms, click mark 0.2 ms after release, no console errors (the browser ran on the host's own computer, so the first frame has no network in it). Tapwright's checked in headless Edge with video. Firefox and Safari not tried | try both; measure the first frame from another computer |
| Both | Wi-Fi and 5G | Tapline's suites ran over the local network; Tapwright's over USB, and its speed and viewer suites over Wi-Fi (the row above). No run over 5G yet: it needs Wi-Fi switched off on the phone | run all three suites on each launch link before launch |

## Later: weak links

Not a launch target. When weak links come back, these were version 1's 3G targets (the iPhone's Network
Link Conditioner "3G" profile: about 780 kbit/s to the phone, 330 kbit/s from it, 100 ms delay): every
action completes and nothing times out; S1 median ≤ 1.5 s, S2 ≤ 1.5 s, S3 ≤ 2.5 s, S4 ≤ 6 s, S5 ≤ 8 s;
V1 median ≤ 1.0 s, V2 ≥ 2 fps, V3 ≤ 2 s, V7 ≤ 33 KB/s of frames. A JPEG stream can't meet V2 there
(the smallest frame measured was 58 KB); it needs a video encoder on the phone.

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

The bar changes only by agreement between the two projects, like VIEWER-PROTOCOL.md (the user handed
these decisions to the two projects on 2026-11-10). A target can rise any time both tools meet it; it doesn't fall because a
tool can't meet it.
