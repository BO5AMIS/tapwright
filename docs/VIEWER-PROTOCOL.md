# Live viewer protocol

Shared by Tapline and Tapwright, so one viewer page (and later an adapter for T3 Code's
Device panel) can show either tool's live picture of a phone, with an overlay of the
elements and input from a person. Version 1, agreed between the two projects; not
published. Version 1.1 (2026-11-09) adds the error codes `not_actionable` and `internal`;
agreed between the two projects and approved by the user. Version 1.2 (2026-11-10) adds a
video transport (`WS /video`) next to the JPEG stream, the state `not-answering`, the error
code `not_answering`, and the page at `/viewer` as well as `/`; the user handed these
decisions to the two projects on 2026-11-10, and both agree on it. Version 1.3 (2026-11-10)
adds `tool` to the status and `overlay` to `accepts`, for the one viewer page both tools now
serve (`viewer.html`, identical in both repos). Version 1.4 (2026-11-11) adds an optional `caption` to
`action` messages: a short plain-language name for the step, which the page shows to people watching. It also
defines `kind`. Without a caption the page makes one from `kind` and `label`. Agreed between the two projects
for the owner's presentation view.

- **Server:** a tool's own process: Tapline's host, or `tapwright serve`.
- **Client:** the viewer page, or an adapter.

## Access

- HTTP and WebSocket on the server's viewer address: `127.0.0.1` by default, the computer's
  Tailscale address only when the server is started with its remote-viewer option.
- Every request carries the server's **viewer token**. The token is separate from any app or
  device secret and is never written to logs.
  - Non-browser clients (adapters, scripts) send `Authorization: Bearer <token>` on every
    request, including the WebSocket handshake.
  - `?token=<token>` is accepted only on the page load that sets the cookie. The server
    answers that load with the page itself and sets the cookie (`HttpOnly; SameSite=Strict;
    Path=/`), with `Referrer-Policy: no-referrer` and `Cache-Control: no-store`; the page
    then removes the query from the address bar (`history.replaceState`). Not a redirect:
    when the link is clicked on another site, Chrome sends the redirected request as
    `cross-site` and without the `Strict` cookie, and a reload stays that way (measured), so
    the page would never load. No other request accepts the token in the URL.
  - A browser page served by the server itself authenticates `/stream`, `/video` and `/ws`
    with that cookie.
  - The server's own viewer page answers at `/viewer` and at `/`, with the same token rules.
  - A request authenticated by the cookie is refused when it carries a `Sec-Fetch-Site`
    header whose value isn't `same-origin` or `none` (cookies are shared across ports, and
    `SameSite` ignores them: a page on `localhost:3000` is the same site).
- Servers check the `Host` header (against DNS rebinding) and, on WebSocket upgrades,
  `Origin`: the page's own origin, or none.
- Action endpoints outside this protocol don't accept the cookie at all (Tapwright's agent
  API at `/api/*` is Bearer only).

## Coordinates and time

- Positions are `0..1` of the frame **as shown, upright**; the server handles orientation.
  Sizes in `0..1` are fractions of the frame's width and height.
- Times (`X-Captured-At`, `at`) are the server's clock in epoch milliseconds.

## `GET /stream`

`multipart/x-mixed-replace; boundary=frame`. Each part is one JPEG with `Content-Type:
image/jpeg`, `Content-Length`, and:

| Header | Meaning |
|---|---|
| `X-Seq` | increasing number, per server |
| `X-Captured-At` | when the frame was captured, if the server knows it; otherwise when the server received it |
| `X-Coverage` | `screen` (the whole phone screen) or `app-window` (only the app's own window) |
| `X-Source` | `wda` or `viewshot` |
| `X-Tree-Rev` | optional: the element-list revision the frame matches |
| `X-Size` | optional: `<w>x<h>`, the frame's size in points |

Latest frame wins: a slow client gets the newest frame and skips stale ones, never falling
behind. A server may stop capturing while no client watches. An `img` element can show the
stream as is; a `fetch`-based client reads the headers.

## `WS /video`

The same picture as `/stream`, as H.264 video. A server offers it when its status lists
`video` in `accepts`; `/stream` stays for every client that can't decode video, and a server
may stop producing JPEGs while only video clients watch. It is authenticated like `/ws`.

**Server to client**

- **Config**, a JSON text message, first on every connection and again whenever the
  picture's size or the encoder's parameters change:
  `{ "type": "config", "codec": "avc1.64001f", "width": 660, "height": 1434, "description": "<base64>", "at": 1731222000000, "coverage": "screen", "source": "airplay" }`
  - `codec`: the WebCodecs codec string, from the stream's profile and level.
  - `width`, `height`: the coded size in pixels, upright.
  - `description`: the `avcC` record (SPS and PPS), base64. Pictures carry no parameter sets.
  - `at`: the server's clock, epoch ms, for the time 0 of the pictures that follow.
  - `coverage`, `source`: as `X-Coverage` and `X-Source` on `/stream`. Sources so far:
    `wda`, `viewshot`, `airplay`, `capture`.
- **Picture**, a binary message: an 8-byte header, then one H.264 access unit in AVCC form
  (each NAL unit preceded by its 4-byte big-endian length).

  | Bytes | |
  |---|---|
  | 0 | flags: bit 0 set = key frame. Other bits are 0 and ignored |
  | 1–3 | 0, ignored |
  | 4–7 | capture time in ms since the config's `at`, unsigned, big-endian. If the server doesn't know when the picture was captured, the time it received it |

- No B-frames and no reordering: pictures are sent in the order they are shown, each as
  soon as it is encoded. No audio.
- The first picture after a config is a key frame.
- Latest wins here too: when a client is slow, the server drops pictures for it and
  continues with a key frame, never queueing more than about a quarter of a second.
- A still screen may send no pictures at all. The client keeps showing the last one, and
  silence on `/video` is not a stall; `/ws` status says whether the link is up.

**Client to server**

| Message | |
|---|---|
| `{ "type": "key" }` | ask for a key frame (after a decode error). A server that can't force one sends the next key frame as it comes |

A client decodes with WebCodecs (`VideoDecoder`, configured with `codec`, `codedWidth`,
`codedHeight` and `description`, and `optimizeForLatency`) and draws each frame at once. If
`VideoDecoder` is missing, the configuration isn't supported, or `/video` closes, the client
uses `/stream`. `quality` presets apply to `/video` as to `/stream`; the server picks the
bitrate.

## `WS /ws`

JSON text messages. Both sides ignore types and fields they don't know.

**Client to server**

| Message | |
|---|---|
| `{ "type": "gesture", "points": [[t_ms, x, y], …] }` | one pointer's path (no multi-touch), sent on release; `t_ms` starts at 0 on the first point. The server decides: tap (short, little movement), long press (held 500 ms or more), drag. |
| `{ "type": "scroll", "x", "y", "dx", "dy" }` | scroll at `x`, `y` by `dx`, `dy` (fractions of the frame); `dy > 0` shows content further down, like a mouse wheel |
| `{ "type": "text", "text": "…" }` | type into the focused field |
| `{ "type": "key", "key": "backspace" \| "enter" }` | |
| `{ "type": "button", "name": "home" \| "volumeUp" \| "volumeDown" }` | |
| `{ "type": "pause", "on": true }` | pause or resume the agent |
| `{ "type": "overlay", "on": true }` | send overlays (`false` to stop); servers send none until asked, since reading the elements can be slow |
| `{ "type": "quality", "preset": "sharp" \| "balanced" \| "light" }` | stream quality, for everyone watching |

**Server to client**

| Message | |
|---|---|
| `{ "type": "status", "protocol": 1, "tool", "state", "detail", "via", "fps", "driver", "paused", "size": { "w", "h" }, "accepts": […], "quality" }` | `tool`: optional, the server's name for the page's title (`Tapline`, `Tapwright`); the page shows `<tool> live view`. `state`: `live`, `connecting`, `reconnecting`, `locked` (the phone is locked; the server can't unlock it; the page shows "Phone locked" and then `detail`), `not-answering` (the app in front is there but doesn't answer; `detail` names it, and the page shows "App not answering") or `no-device`. `detail`: optional plain text (needs setup, phone not found …). `via`: how the phone is reached (`usb`, `wifi`, `tailscale`). `driver`: `agent`, `person` or `null`. `size` in points. `accepts`: the input this server supports, from `tap`, `longPress`, `drag`, `scroll`, `text`, `enter`, `backspace`, `home`, `volume`, `quality`, `video` (the server offers `/video`) and `overlay` (the server can send overlays); clients hide the rest. `quality`: optional, the current stream preset (`sharp`, `balanced`, `light`) when the server accepts `quality`. |
| `{ "type": "overlay", "rev", "at", "elements": [{ "ref", "type", "label", "id", "rect": { "x", "y", "w", "h" } }] }` | the elements on screen while overlays are on. `rev` matches `X-Tree-Rev` when the server has revisions; `at` is when it was read, so a client can dim an overlay older than the latest action. |
| `{ "type": "action", "by": "agent" \| "person", "kind", "label", "caption", "rect", "point", "at", "ok", "code", "reason" }` | What was just done, for the viewer to show: a highlight or touch mark, and a caption. `rect`, `point` (in 0..1), `at`, `ok`, `label`, `caption` and `reason` are optional; when `ok` is `false`, `code` says why (see errors), and with `code` `not_actionable`, `reason` is the reason word from AGENT-TOOLS §4 (`covered`, `off_screen`, `disabled`, `under_tab_bar`, `behind_alert`, `no_action`). `kind` is the step's name (see "Kinds"). `label` is the element's name, for the page's details view. `caption` is the step in plain words, for people (see "Captions"). |
| `{ "type": "error", "message", "code" }` | `code`, optional and machine-readable: `busy` (a person is driving), `paused`, `stale` (the screen changed; look again), `locked`, `reconnecting`, `not_answering` (the app in front doesn't answer; close it and open it again), `unsupported`, `unauthorized`, `not_actionable` (the input reached the app, but a real touch there would do nothing or couldn't reach what's there; the message starts with the reason word, as in AGENT-TOOLS.md §4, e.g. `no_action: Nothing at (120, 640) takes a touch.`), `internal` (a bug or a failure on the phone side; the message says which) |

## Steps: kinds and captions

### Kinds

- **For the agent's steps,** `kind` is the AGENT-TOOLS tool name: `tap`, `long_press`, `type_text`, `scroll`,
  `swipe`, `toggle`, `back`, `alert`, `open_url`, `open_app`, `close_app`, `rotate`, `reload`, `app_command`,
  `wait_for`.
- **For a person's input,** it is the viewer input name: `tap`, `longPress`, `drag`, `scroll`, `text`, `enter`,
  `backspace`, `home`, `volume`.
- **A page treats a kind it doesn't know** by the table's last row.

### Captions

- **What a caption is:**
  - one short sentence fragment, at most 60 characters, sentence case, no full stop;
  - it names the step as a person would: `Tap "Sign up"`, `Type into "Email"`, `Scroll down`,
    `Switch "Remind me"`, `Answer the alert`, `Go back`, `Open Wayfare`, `Turn the phone`.
- **What a caption never contains:**
  - refs, coordinates, element types, JSON or bundle identifiers;
  - the text typed into a field: the phone shows that itself, and a password must never appear;
  - a link's address: `open_url` is always "Open link", because a URL can carry codes or tokens;
  - the failure reason: the page adds it (see below), so it is never said twice.
- **Element names** longer than 32 characters are cut with "…", by the server and by the page alike.
- **The server's caption** is for the agent's steps. It may say more than the table, e.g. `app_command` →
  "Switch to dark mode". When it sends none, the page uses the table. A person's steps always use the table's
  person column.

| `kind` | Agent's step | Person's step |
|---|---|---|
| `tap`, `double_tap` | `Tap "<label>"` (`Tap` without a label) | `You tapped "<label>"` (`You tapped`) |
| `long_press`, `longPress` | `Press and hold "<label>"` | `You pressed and held "<label>"` |
| `type_text`, `text` | `Type into "<label>"` (`Type` without a label) | `You typed` |
| `enter` | `Press Return` | `You pressed Return` |
| `backspace` | `Delete` | `You deleted` |
| `scroll` | `Scroll` (`Scroll up/down/left/right` when known) | `You scrolled` |
| `swipe`, `drag` | `Swipe` | `You swiped` |
| `toggle` | `Switch "<label>"` | `You switched "<label>"` |
| `back` | `Go back` | `You went back` |
| `alert` | `Answer the alert` | `You answered the alert` |
| `open_url` | `Open link` | `You opened a link` |
| `open_app` / `close_app` | `Open <label>` / `Close <label>` | `You opened <label>` / `You closed <label>` |
| `rotate` | `Turn the phone` | `You turned the phone` |
| `home` | `Home` | `You pressed Home` |
| `volume` | `Volume up` / `Volume down` | `You pressed Volume up` / `down` |
| `reload` | `Reload the app` | `You reloaded the app` |
| `app_command` | `Run "<label>"` | `You ran "<label>"` |
| `wait_for` | no caption, no pill (not a step a viewer needs) | — |
| anything else | the `kind`, with `_` as spaces | `You: ` and the `kind`, with `_` as spaces |

- **Failures:** when `ok` is `false`, the page shows the caption with the reason after a dash:
  `Tap "Save" – covered`. The reason is `reason` when the message has one, else `code`. The wording is fixed:

| Code or reason word | Shown as |
|---|---|
| `covered` | covered |
| `off_screen` | off screen |
| `disabled` | disabled |
| `under_tab_bar` | under the tab bar |
| `behind_alert` | behind an alert |
| `no_action` | nothing there |
| `stale` | the screen changed |
| `busy` | someone else is using it |
| `paused` | paused |
| `not_answering` | app not answering |
| `timeout` | took too long |
| anything else | didn't work |

### The page's presentation view

These are listed so both tools show them the same way. They are the page's own behaviour.

- **Touches:** a soft ripple at `point`, and a fading trail for a drag, swipe or scroll. No text beside the
  touch.
- **Caption pill:** under the phone, the latest step's caption. It is in the agent's or the person's colour,
  stays for 2.5 s or until the next step, and is red with the reason when `ok` is `false`.
- **Steps drawer,** closed by default: the steps of this session, newest first (the last 50), each with a small
  icon by kind, its caption, and how long ago it happened.
- **Details toggle,** off by default: shows the developer view (the `label` beside a touch, the element boxes
  when `overlay` is accepted). The choice is remembered per browser.
- **Reduced motion:** with `prefers-reduced-motion`, no ripple animation and no trail; a static ring shows
  briefly instead.
- **The caption is announced politely to screen readers** (the existing `announce` region), at most once a
  second.

## Control

One action queue per server, for the agent and people alike. A person's input goes first:
an agent action waits until the person has been idle for about 3 seconds and gives up after
about 30 with `busy`. While paused, agent actions are refused with `paused`. An action by
element ref from before the screen changed is refused with `stale`; by id, label or text it
is looked up again.

## Per tool

| | Tapwright | Tapline |
|---|---|---|
| `X-Source` / `X-Coverage` | `wda` / `screen` | `viewshot` / `app-window` |
| Frames (`/stream`) | up to about 27 a second at the preset's rate while something happens (an agent's call in the last 30 s, a person's input, a changing screen), 5 a second otherwise (measured) | about 10 a second for a second after each action or tap, about 6 a second while the screen keeps changing, one a second when still (measured); a capture takes about 25–30 ms on an iPhone |
| Video (`/video`) | from the phone's own AirPlay mirror, received and re-encoded on the computer (Wi-Fi, same network): `source` `airplay`, `coverage` `screen`. Offered only while mirroring runs | not yet: a capture source inside the app can join later (`source` `capture`, `coverage` `app-window`) with no protocol change |
| `not-answering` | the app in front doesn't answer accessibility queries | the app is connected but sent nothing for 45 s, or didn't answer a call, while not in the background |
| `locked` | the phone is locked | also while the app is in the background (iOS pauses it; only the person can bring it back) |
| `X-Tree-Rev` | none | yes (React commits) |
| Overlay | slow to read (1.5 to 7 s on busy screens, measured), so only on request | quick, on request |
| `accepts` | everything | `tap`, `longPress`, `scroll`, `text`, `enter`, `backspace`, `quality` (capture size) |
| Sees | the whole screen: keyboard, status bar, system alerts | only the app's window: no keyboard, status bar or system alerts |
| A person's input refused | never `not_actionable`: viewer taps are real touches | `not_actionable` with the reason (`no_action`, `covered`, `under_tab_bar`, `disabled` …), since Tapline calls handlers instead of touching |

T3 Code's Device panel is reached through a separate adapter in each tool, not through this
protocol.
